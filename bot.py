"""
Telegram bot <-> GitHub manager.
Deploy di Railway sebagai worker (long polling, tidak butuh domain).
"""
import asyncio
import base64
import html
import io
import json
import logging
import os
import posixpath
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("tg-github-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# --------------------------------------------------------------------------- #
# Konfigurasi
# --------------------------------------------------------------------------- #
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x.strip().isdigit()
}
DEFAULT_REPO = os.environ.get("DEFAULT_REPO", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # batas bot API: download file dari user
TG_UPLOAD_LIMIT = 49 * 1024 * 1024  # batas bot API: kirim file ke chat (50MB)
MAX_ZIP_FILES = 3000
MAX_UNZIPPED = 200 * 1024 * 1024

API = "https://api.github.com"

# --------------------------------------------------------------------------- #
# Util
# --------------------------------------------------------------------------- #
class UserError(Exception):
    """Error yang pesannya aman ditampilkan ke user."""


class TooBig(Exception):
    pass


def esc(s) -> str:
    return html.escape(str(s))


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


JUNK = {".ds_store", "thumbs.db"}


def clean_path(p: str):
    """Normalisasi path; None kalau tidak valid / harus dilewati."""
    p = (p or "").replace("\\", "/").strip()
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        return None
    if parts[0] == "__MACOSX" or ".git" in parts:
        return None
    if parts[-1].lower() in JUNK:
        return None
    return "/".join(parts)


def clean_folder(caption) -> str:
    if not caption:
        return ""
    c = caption.strip().splitlines()[0].strip()
    if not c or c in ("/", "."):
        return ""
    r = clean_path(c)
    if r is None:
        raise UserError("Nama folder tujuan (caption) tidak valid.")
    return r


def extract_zip(data: bytes, base_dir: str) -> dict:
    files = {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UserError("File zip rusak / bukan zip yang valid.")
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise UserError(f"Terlalu banyak file di zip (maks {MAX_ZIP_FILES}).")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise UserError(
                f"Ukuran hasil ekstrak melebihi {fmt_size(MAX_UNZIPPED)}."
            )
        items = []
        for i in infos:
            n = clean_path(i.filename)
            if n:
                items.append((i, n))
        if items:  # buang folder pembungkus tunggal (root) jika ada
            roots = {n.split("/")[0] for _, n in items}
            if len(roots) == 1 and all("/" in n for _, n in items):
                items = [(i, n.split("/", 1)[1]) for i, n in items]
        for i, n in items:
            full = posixpath.join(base_dir, n) if base_dir else n
            try:
                files[full] = zf.read(i)
            except RuntimeError:
                raise UserError("Zip terenkripsi/berpassword tidak didukung.")
    return files


def read_limited(r: requests.Response, cap: int) -> io.BytesIO:
    try:
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > cap:
            raise TooBig()
        buf, n = io.BytesIO(), 0
        for chunk in r.iter_content(1 << 20):
            n += len(chunk)
            if n > cap:
                raise TooBig()
            buf.write(chunk)
        buf.seek(0)
        return buf
    finally:
        r.close()


REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def parse_target(text: str):
    """Return (repo, kind, rest_segments). kind: repo|tree|blob."""
    text = text.strip()
    if "github.com" in text:
        m = re.search(r"https?://(?:www\.)?github\.com/[^\s]+", text)
        if not m:
            raise UserError("Link GitHub tidak valid.")
        segs = [s for s in urlparse(m.group(0)).path.split("/") if s]
        if len(segs) < 2:
            raise UserError("Link harus berisi owner/repo.")
        name = re.sub(r"\.git$", "", segs[1])
        repo = f"{segs[0]}/{name}"
        if len(segs) > 3 and segs[2] in ("tree", "blob", "raw"):
            kind = "blob" if segs[2] in ("blob", "raw") else "tree"
            return repo, kind, segs[3:]
        return repo, "repo", []
    text = re.sub(r"\.git$", "", text)
    if not REPO_RE.match(text):
        raise UserError("Format harus owner/repo atau link GitHub.")
    return text, "repo", []


# --------------------------------------------------------------------------- #
# GitHub client
# --------------------------------------------------------------------------- #
class GitHubError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


class GH:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "telegram-github-bot",
            }
        )

    def req(self, method, path, ok=(200, 201), **kw):
        url = path if path.startswith("http") else API + path
        timeout = kw.pop("timeout", 60)
        for attempt in range(4):
            r = self.s.request(method, url, timeout=timeout, **kw)
            if (
                r.status_code in (403, 429)
                and attempt < 3
                and any(k in r.text.lower() for k in ("rate limit", "abuse"))
            ):
                wait = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
                time.sleep(min(wait, 60))
                continue
            break
        if r.status_code not in ok:
            try:
                msg = r.json().get("message", r.text[:200])
            except Exception:
                msg = r.text[:200]
            raise GitHubError(f"{r.status_code}: {msg}", r.status_code)
        return r

    # ---- repo ----
    def me(self):
        return self.req("GET", "/user").json()

    def get_repo(self, repo):
        return self.req("GET", f"/repos/{repo}").json()

    def list_repos(self):
        return self.req(
            "GET", "/user/repos", params={"per_page": 50, "sort": "updated"}
        ).json()

    def create_repo(self, name, private, desc):
        return self.req(
            "POST",
            "/user/repos",
            json={
                "name": name,
                "private": private,
                "description": desc,
                "auto_init": True,
            },
        ).json()

    # ---- branch ----
    def list_branches(self, repo):
        return self.req(
            "GET", f"/repos/{repo}/branches", params={"per_page": 100}, ok=(200, 404, 409)
        ).json()

    def branch_sha(self, repo, branch):
        r = self.req(
            "GET",
            f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}",
            ok=(200, 404, 409),
        )
        return r.json()["object"]["sha"] if r.status_code == 200 else None

    def create_branch(self, repo, name, from_branch):
        if self.branch_sha(repo, name):
            raise UserError(f"Branch '{name}' sudah ada.")
        sha = self.branch_sha(repo, from_branch)
        if not sha:
            raise UserError(f"Branch asal '{from_branch}' tidak ditemukan.")
        self.req(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{name}", "sha": sha},
        )

    # ---- files ----
    def put_file(self, repo, path, data: bytes, branch, message):
        url = f"/repos/{repo}/contents/{quote(path, safe='/')}"
        params = {"ref": branch} if branch else {}
        cur = self.req("GET", url, params=params, ok=(200, 404, 409))
        sha = None
        if cur.status_code == 200:
            j = cur.json()
            if isinstance(j, list):
                raise UserError(f"'{path}' adalah folder di repo.")
            sha = j["sha"]
        body = {"message": message, "content": base64.b64encode(data).decode()}
        if branch:
            body["branch"] = branch
        if sha:
            body["sha"] = sha
        r = self.req("PUT", url, json=body, timeout=120).json()
        return r, bool(sha)

    def commit_files(self, repo, branch, files: dict, message):
        """Satu commit untuk banyak file (Git Data API). Return (sha, branch)."""
        head = self.branch_sha(repo, branch)
        if head is None:
            if self.list_branches(repo):
                raise GitHubError(f"Branch '{branch}' tidak ditemukan", 404)
            # repo kosong: bootstrap dengan satu file via Contents API
            first = next(iter(files))
            res, _ = self.put_file(repo, first, files[first], None, message)
            branch = self.get_repo(repo)["default_branch"]
            files = {k: v for k, v in files.items() if k != first}
            head = res["commit"]["sha"]
            if not files:
                return head, branch

        base_tree = self.req("GET", f"/repos/{repo}/git/commits/{head}").json()["tree"][
            "sha"
        ]

        def mk(item):
            path, data = item
            r = self.req(
                "POST",
                f"/repos/{repo}/git/blobs",
                json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
                timeout=120,
            )
            return {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": r.json()["sha"],
            }

        with ThreadPoolExecutor(4) as ex:
            entries = list(ex.map(mk, files.items()))
        return self._commit_tree(repo, branch, head, base_tree, entries, message), branch

    def _commit_tree(self, repo, branch, head, base_tree, entries, message):
        tree = self.req(
            "POST",
            f"/repos/{repo}/git/trees",
            json={"base_tree": base_tree, "tree": entries},
            timeout=120,
        ).json()["sha"]
        commit = self.req(
            "POST",
            f"/repos/{repo}/git/commits",
            json={"message": message, "tree": tree, "parents": [head]},
        ).json()["sha"]
        self.req(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{quote(branch, safe='/')}",
            json={"sha": commit},
        )
        return commit

    def delete_path(self, repo, branch, path):
        """Hapus file atau folder (rekursif) dalam satu commit."""
        head = self.branch_sha(repo, branch)
        if not head:
            raise UserError(f"Branch '{branch}' tidak ditemukan.")
        base_tree = self.req("GET", f"/repos/{repo}/git/commits/{head}").json()["tree"][
            "sha"
        ]
        tree = self.req(
            "GET", f"/repos/{repo}/git/trees/{base_tree}", params={"recursive": 1}
        ).json()
        targets = [
            t["path"]
            for t in tree["tree"]
            if t["type"] == "blob" and (t["path"] == path or t["path"].startswith(path + "/"))
        ]
        if not targets:
            raise UserError(f"'{path}' tidak ditemukan di branch {branch}.")
        entries = [
            {"path": p, "mode": "100644", "type": "blob", "sha": None} for p in targets
        ]
        sha = self._commit_tree(
            repo, branch, head, base_tree, entries, f"Delete {path} via Telegram bot"
        )
        return sha, len(targets)

    def list_dir(self, repo, path, branch):
        url = f"/repos/{repo}/contents/{quote(path, safe='/')}".rstrip("/")
        return self.req("GET", url, params={"ref": branch}).json()

    def file_bytes(self, repo, path, branch, cap):
        url = f"/repos/{repo}/contents/{quote(path, safe='/')}"
        r = self.req(
            "GET",
            url,
            params={"ref": branch},
            headers={"Accept": "application/vnd.github.raw+json"},
            stream=True,
            timeout=120,
        )
        return read_limited(r, cap)

    def zipball(self, repo, ref, cap):
        url = f"/repos/{repo}/zipball" + (f"/{quote(ref, safe='/')}" if ref else "")
        r = self.req("GET", url, stream=True, timeout=120)
        return read_limited(r, cap)


gh = GH(GITHUB_TOKEN)


def run(fn, *a, **k):
    return asyncio.to_thread(fn, *a, **k)


# --------------------------------------------------------------------------- #
# State per user (repo & branch aktif)
# --------------------------------------------------------------------------- #
_state: dict = {}
STATE_FILE = DATA_DIR / "state.json"


def load_state():
    global _state
    try:
        _state = json.loads(STATE_FILE.read_text())
    except Exception:
        _state = {}


def save_state():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(_state))
    except Exception as e:
        log.warning("Gagal simpan state: %s", e)


def get_state(uid) -> dict:
    return _state.get(str(uid), {})


def set_state(uid, **kw):
    st = _state.setdefault(str(uid), {})
    if "repo" in kw and kw["repo"] != st.get("repo"):
        st["dir"] = ""
    st.update(kw)
    save_state()


async def resolve(uid):
    st = get_state(uid)
    repo = st.get("repo") or DEFAULT_REPO
    if not repo:
        raise UserError(
            "Belum ada repo aktif. Pakai /repos, /setrepo owner/repo, atau /newrepo nama."
        )
    branch = st.get("branch")
    if not branch:
        branch = (await run(gh.get_repo, repo))["default_branch"]
    return repo, branch


async def resolve_ref(repo, segs):
    """Pisahkan 'branch/path' dari link GitHub (branch boleh mengandung '/')."""
    if not segs:
        return (await run(gh.get_repo, repo))["default_branch"], ""
    names = {b["name"] for b in await run(gh.list_branches, repo) if isinstance(b, dict)}
    for i in range(len(segs), 0, -1):
        cand = "/".join(segs[:i])
        if cand in names:
            return cand, "/".join(segs[i:])
    return segs[0], "/".join(segs[1:])


# --------------------------------------------------------------------------- #
# Helper Telegram
# --------------------------------------------------------------------------- #
def err_text(e: Exception) -> str:
    if isinstance(e, UserError):
        return f"⚠️ {esc(e)}"
    if isinstance(e, GitHubError):
        hint = ""
        if e.status in (401, 403):
            hint = "\nCek scope/izin GitHub token."
        return f"❌ GitHub: {esc(e)}{hint}"
    return "❌ Terjadi kesalahan tak terduga. Cek log di Railway."


def restricted(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ALLOWED:
            if update.callback_query:
                await update.callback_query.answer("Akses ditolak", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ Akses ditolak. ID kamu: {user.id if user else '?'}"
                )
            return
        try:
            await fn(update, ctx)
        except Exception as e:
            if not isinstance(e, (UserError, GitHubError)):
                log.exception("Handler error")
            if update.effective_message:
                await update.effective_message.reply_text(
                    err_text(e), parse_mode=ParseMode.HTML
                )

    return wrapper


async def reply(update: Update, text: str, markup=None):
    return await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


async def safe_edit(msg, text, markup=None):
    try:
        await msg.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


def kb(rows):
    return InlineKeyboardMarkup(rows)


def btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
HELP = """<b>🤖 GitHub Manager Bot</b>

<b>Upload</b>
• Kirim file apa saja → diupload ke repo aktif
• Kirim <code>.zip</code> → pilih <b>Ekstrak</b> (isi zip langsung ke tujuan, tanpa folder pembungkus) atau <b>Upload zip asli</b>
• Tujuan (repo/branch/folder) selalu ditampilkan sebelum upload
• Ganti folder: caption file (mis. <code>src/assets</code>) atau /setdir folder

<b>Repo &amp; branch</b>
/repos – pilih dari daftar repo kamu
/setrepo owner/repo – set repo aktif
/newrepo nama [public|private] [deskripsi]
/branches – lihat &amp; ganti branch
/setbranch nama
/newbranch nama [dari_branch]
/status – tujuan upload saat ini
/setdir folder – folder tujuan default (kosong = root)

<b>Download &amp; kelola</b>
/ls [folder] – jelajah isi repo (tombol)
/get path/file – download 1 file
/dl [link|owner/repo] [branch] – download repo full zip
/delete path – hapus file/folder (dengan konfirmasi)

💡 Kirim link GitHub langsung ke chat → muncul tombol aksi."""


@restricted
async def cmd_start(update, ctx):
    await reply(update, HELP)


@restricted
async def cmd_status(update, ctx):
    uid = update.effective_user.id
    repo, branch = await resolve(uid)
    me = await run(gh.me)
    await reply(
        update,
        f"👤 GitHub: <code>{esc(me['login'])}</code>\n\n"
        f"<b>Tujuan upload saat ini:</b>\n"
        f"{dest_text(repo, branch, get_state(uid).get('dir', ''))}",
    )


@restricted
async def cmd_setdir(update, ctx):
    uid = update.effective_user.id
    repo, branch = await resolve(uid)
    arg = " ".join(ctx.args).strip()
    if arg in ("", "/", ".", "root"):
        folder = ""
    else:
        folder = clean_path(arg)
        if not folder:
            raise UserError("Nama folder tidak valid.")
    set_state(uid, dir=folder)
    await reply(
        update,
        f"✅ Folder tujuan default diubah.\n\n{dest_text(repo, branch, folder)}\n\n"
        f"<i>Caption pada file tetap menimpa pengaturan ini.</i>",
    )


@restricted
async def cmd_repos(update, ctx):
    repos = await run(gh.list_repos)
    if not repos:
        raise UserError("Belum ada repo. Buat dengan /newrepo nama.")
    ctx.user_data["repos"] = [r["full_name"] for r in repos]
    rows = [
        [btn(("🔒 " if r["private"] else "🌐 ") + r["full_name"], f"repo:{i}")]
        for i, r in enumerate(repos)
    ]
    await reply(update, "Pilih repo aktif (50 terbaru):", kb(rows))


@restricted
async def cmd_setrepo(update, ctx):
    if not ctx.args:
        raise UserError("Pemakaian: /setrepo owner/repo")
    repo, _, _ = parse_target(ctx.args[0])
    info = await run(gh.get_repo, repo)
    set_state(
        update.effective_user.id, repo=info["full_name"], branch=info["default_branch"]
    )
    await reply(
        update,
        f"✅ Repo aktif: <code>{esc(info['full_name'])}</code>\n"
        f"🌿 Branch: <code>{esc(info['default_branch'])}</code>",
    )


@restricted
async def cmd_newrepo(update, ctx):
    if not ctx.args:
        raise UserError("Pemakaian: /newrepo nama [public|private] [deskripsi]")
    name, rest = ctx.args[0], list(ctx.args[1:])
    if not re.match(r"^[\w.-]+$", name):
        raise UserError("Nama repo hanya boleh huruf, angka, '-', '_' dan '.'")
    private = True
    if rest and rest[0].lower() in ("public", "private"):
        private = rest.pop(0).lower() == "private"
    info = await run(gh.create_repo, name, private, " ".join(rest))
    set_state(
        update.effective_user.id, repo=info["full_name"], branch=info["default_branch"]
    )
    await reply(
        update,
        f"✅ Repo dibuat &amp; dijadikan aktif:\n"
        f"<a href='{esc(info['html_url'])}'>{esc(info['full_name'])}</a> "
        f"({'🔒 private' if private else '🌐 public'})",
    )


@restricted
async def cmd_branches(update, ctx):
    repo, current = await resolve(update.effective_user.id)
    branches = await run(gh.list_branches, repo)
    if not isinstance(branches, list) or not branches:
        raise UserError("Repo ini belum punya branch (kosong). Upload file dulu.")
    names = [b["name"] for b in branches]
    ctx.user_data["branches"] = names
    rows = [
        [btn(("✅ " if n == current else "🌿 ") + n, f"br:{i}")]
        for i, n in enumerate(names)
    ]
    await reply(update, f"Branch di <code>{esc(repo)}</code>:", kb(rows))


@restricted
async def cmd_setbranch(update, ctx):
    if not ctx.args:
        raise UserError("Pemakaian: /setbranch nama")
    repo, _ = await resolve(update.effective_user.id)
    name = " ".join(ctx.args)
    if not await run(gh.branch_sha, repo, name):
        raise UserError(f"Branch '{name}' tidak ada. Buat dengan /newbranch.")
    set_state(update.effective_user.id, repo=repo, branch=name)
    await reply(update, f"✅ Branch aktif: <code>{esc(name)}</code>")


@restricted
async def cmd_newbranch(update, ctx):
    if not ctx.args:
        raise UserError("Pemakaian: /newbranch nama [dari_branch]")
    uid = update.effective_user.id
    repo, current = await resolve(uid)
    name = ctx.args[0]
    base = ctx.args[1] if len(ctx.args) > 1 else current
    await run(gh.create_branch, repo, name, base)
    set_state(uid, repo=repo, branch=name)
    await reply(
        update,
        f"✅ Branch <code>{esc(name)}</code> dibuat dari <code>{esc(base)}</code> "
        f"dan dijadikan aktif.",
    )


# ---- ls / get ----
async def show_ls(update, ctx, repo, branch, path):
    items = await run(gh.list_dir, repo, path, branch)
    if isinstance(items, dict):  # ternyata file
        await send_repo_file(update, repo, branch, path)
        return
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    entries = []
    if path:
        parent = posixpath.dirname(path)
        entries.append({"path": parent, "type": "dir", "label": "⬆️ .."})
    for it in items[:40]:
        icon = "📁" if it["type"] == "dir" else "📄"
        entries.append({"path": it["path"], "type": it["type"], "label": f"{icon} {it['name']}"})
    ctx.user_data["ls"] = entries
    rows = [[btn(e["label"], f"ls:{i}")] for i, e in enumerate(entries)]
    extra = f"\n(menampilkan 40 dari {len(items)})" if len(items) > 40 else ""
    where = f"/{path}" if path else "/"
    await reply(
        update,
        f"📂 <code>{esc(repo)}</code> @ <code>{esc(branch)}</code>\n"
        f"<code>{esc(where)}</code>{extra}",
        kb(rows) if rows else None,
    )


@restricted
async def cmd_ls(update, ctx):
    repo, branch = await resolve(update.effective_user.id)
    path = clean_path(" ".join(ctx.args)) or "" if ctx.args else ""
    await show_ls(update, ctx, repo, branch, path)


async def send_repo_file(update, repo, branch, path):
    msg = update.effective_message
    status = await msg.reply_text(f"⏳ Mengambil {path} ...")
    meta = await run(gh.list_dir, repo, path, branch)
    if isinstance(meta, list):
        await status.delete()
        raise UserError("Itu folder. Pakai /ls untuk menjelajah atau /dl untuk zip repo.")
    size = meta.get("size", 0)
    if size == 0:
        await status.delete()
        raise UserError("File kosong (0 byte), Telegram tidak bisa mengirim file kosong.")
    if size > TG_UPLOAD_LIMIT:
        await status.delete()
        raise UserError(f"File {fmt_size(size)} melebihi batas kirim Telegram (50 MB).")
    try:
        data = await run(gh.file_bytes, repo, path, branch, TG_UPLOAD_LIMIT)
    except TooBig:
        await status.delete()
        raise UserError("File melebihi batas kirim Telegram (50 MB).")
    await msg.reply_document(
        document=data,
        filename=posixpath.basename(path),
        caption=f"{repo}@{branch}:{path}"[:1000],
        write_timeout=300,
    )
    await status.delete()


@restricted
async def cmd_get(update, ctx):
    if not ctx.args:
        raise UserError("Pemakaian: /get path/ke/file")
    path = clean_path(" ".join(ctx.args))
    if not path:
        raise UserError("Path tidak valid.")
    repo, branch = await resolve(update.effective_user.id)
    await send_repo_file(update, repo, branch, path)


# ---- download repo ----
async def send_repo_zip(update, repo, ref):
    msg = update.effective_message
    status = await msg.reply_text(f"⏳ Mengunduh {repo}{('@' + ref) if ref else ''} ...")
    try:
        data = await run(gh.zipball, repo, ref, TG_UPLOAD_LIMIT)
    except TooBig:
        await status.delete()
        raise UserError(
            "Repo lebih dari 50 MB, melebihi batas kirim Telegram.\n"
            f"Download manual: https://github.com/{repo}/archive/refs/heads/{ref or 'HEAD'}.zip"
        )
    name = repo.split("/")[1] + (f"-{ref.replace('/', '-')}" if ref else "") + ".zip"
    await msg.reply_document(
        document=data, filename=name, caption=f"📦 {repo}", write_timeout=300
    )
    await status.delete()


@restricted
async def cmd_dl(update, ctx):
    args = ctx.args
    uid = update.effective_user.id
    if args and ("github.com" in args[0] or "/" in args[0]):
        repo, kind, rest = parse_target(args[0])
        if len(args) > 1:
            ref = " ".join(args[1:])
        elif kind in ("tree", "blob"):
            ref, _ = await resolve_ref(repo, rest)
        else:
            ref = None
    else:
        repo, branch = await resolve(uid)
        ref = " ".join(args) if args else branch
    await send_repo_zip(update, repo, ref)


# ---- delete ----
@restricted
async def cmd_delete(update, ctx):
    path = clean_path(" ".join(ctx.args)) if ctx.args else None
    if not path:
        raise UserError("Pemakaian: /delete path/file_atau_folder")
    repo, branch = await resolve(update.effective_user.id)
    ctx.user_data["del"] = path
    await reply(
        update,
        f"⚠️ Hapus <code>{esc(path)}</code> dari <code>{esc(repo)}</code>@"
        f"<code>{esc(branch)}</code>?\nFolder akan dihapus beserta isinya.",
        kb([[btn("🗑 Ya, hapus", "del:y"), btn("Batal", "del:n")]]),
    )


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
def dest_text(repo, branch, folder):
    where = f"/{folder}" if folder else "/ (root repo)"
    return (
        f"📦 Repo: <code>{esc(repo)}</code>\n"
        f"🌿 Branch: <code>{esc(branch)}</code>\n"
        f"📁 Folder: <code>{esc(where)}</code>"
    )


async def do_upload(ctx, status, file_id, name, folder, mode, repo, branch):
    dest = dest_text(repo, branch, folder)
    try:
        await safe_edit(
            status, f"⬇️ Mengunduh <code>{esc(name)}</code> dari Telegram ...\n\n{dest}"
        )
        tg_file = await ctx.bot.get_file(file_id)
        data = bytes(await tg_file.download_as_bytearray())

        if mode == "r":  # upload file apa adanya
            fname = clean_path(name) or "file"
            path = posixpath.join(folder, fname) if folder else fname
            await safe_edit(status, f"⬆️ Mengunggah ke GitHub ...\n\n{dest}")
            _, existed = await run(
                gh.put_file, repo, path, data, branch, f"Upload {fname} via Telegram bot"
            )
            link = f"https://github.com/{repo}/blob/{quote(branch, safe='/')}/{quote(path, safe='/')}"
            await safe_edit(
                status,
                f"✅ {'Diperbarui' if existed else 'Diupload'}: "
                f"<a href='{esc(link)}'>{esc(path)}</a> ({fmt_size(len(data))})\n\n{dest}",
            )
        else:  # ekstrak: isi zip langsung ke tujuan, tanpa folder pembungkus
            files = await run(extract_zip, data, folder)
            if not files:
                raise UserError("Zip kosong / tidak ada file valid di dalamnya.")
            await safe_edit(status, f"⬆️ Mengunggah {len(files)} file ...\n\n{dest}")
            sha, used = await run(
                gh.commit_files, repo, branch, files, f"Extract {name} via Telegram bot"
            )
            prefix = folder + "/" if folder else ""
            top = sorted({p[len(prefix):].split("/")[0] for p in files})
            shown = ", ".join(top[:8]) + (f" +{len(top) - 8} lagi" if len(top) > 8 else "")
            await safe_edit(
                status,
                f"✅ <b>{len(files)} file</b> diekstrak langsung ke tujuan\n"
                f"Isi: <code>{esc(shown)}</code>\n\n{dest_text(repo, used, folder)}\n"
                f"<a href='https://github.com/{esc(repo)}/commit/{sha}'>Lihat commit</a>",
            )
    except Exception as e:
        if not isinstance(e, (UserError, GitHubError)):
            log.exception("Upload gagal")
        await safe_edit(status, f"{err_text(e)}\n\n{dest}")


@restricted
async def on_attachment(update, ctx):
    msg = update.message
    uid = update.effective_user.id
    if msg.document:
        att, name = msg.document, msg.document.file_name
    elif msg.photo:
        att = msg.photo[-1]
        name = f"photo_{att.file_unique_id}.jpg"
    elif msg.video:
        att, name = msg.video, msg.video.file_name
    else:
        att, name = msg.audio, msg.audio.file_name
    name = name or f"file_{att.file_unique_id}"

    repo, branch = await resolve(uid)
    folder = clean_folder(msg.caption) if msg.caption else get_state(uid).get("dir", "")
    if att.file_size and att.file_size > TG_DOWNLOAD_LIMIT:
        raise UserError(
            f"File {fmt_size(att.file_size)} — bot Telegram hanya bisa mengunduh file ≤ 20 MB."
        )
    dest = dest_text(repo, branch, folder)

    if name.lower().endswith(".zip"):
        key = str(msg.message_id)
        ctx.user_data.setdefault("pending", {})[key] = {
            "file_id": att.file_id,
            "name": name,
            "folder": folder,
            "repo": repo,
            "branch": branch,
        }
        await msg.reply_text(
            f"📦 <b>{esc(name)}</b>\n\n<b>Tujuan upload:</b>\n{dest}\n\n"
            f"Mau diapakan?\n<i>Ganti folder: kirim ulang dengan caption, atau pakai /setdir.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb(
                [
                    [btn("📂 Ekstrak (isi zip langsung ke tujuan)", f"zip:x:{key}")],
                    [btn("📄 Upload zip asli", f"zip:r:{key}")],
                    [btn("✖️ Batal", f"zip:c:{key}")],
                ]
            ),
        )
    else:
        status = await msg.reply_text(
            f"⏳ Memproses ...\n\n<b>Tujuan upload:</b>\n{dest}", parse_mode=ParseMode.HTML
        )
        await do_upload(ctx, status, att.file_id, name, folder, "r", repo, branch)


# --------------------------------------------------------------------------- #
# Link GitHub di chat
# --------------------------------------------------------------------------- #
@restricted
async def on_text(update, ctx):
    text = update.message.text or ""
    if "github.com/" not in text:
        await reply(update, "Kirim file, link GitHub, atau /help untuk daftar perintah.")
        return
    repo, kind, rest = parse_target(text)
    ctx.user_data["link"] = {"repo": repo, "kind": kind, "rest": rest}
    rows = []
    if kind == "blob":
        rows.append([btn("📄 Download file ini", "lk:file")])
    rows.append([btn("📦 Download repo (zip)", "lk:zip")])
    rows.append([btn("📌 Jadikan repo aktif", "lk:set")])
    await reply(update, f"🔗 <code>{esc(repo)}</code> — pilih aksi:", kb(rows))


# --------------------------------------------------------------------------- #
# Callback tombol
# --------------------------------------------------------------------------- #
@restricted
async def on_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    action, _, val = q.data.partition(":")

    if action == "repo":
        repos = ctx.user_data.get("repos", [])
        if not val.isdigit() or int(val) >= len(repos):
            raise UserError("Daftar kadaluarsa, jalankan /repos lagi.")
        info = await run(gh.get_repo, repos[int(val)])
        set_state(uid, repo=info["full_name"], branch=info["default_branch"])
        await safe_edit(
            q.message,
            f"✅ Repo aktif: <code>{esc(info['full_name'])}</code>\n"
            f"🌿 Branch: <code>{esc(info['default_branch'])}</code>",
        )

    elif action == "br":
        names = ctx.user_data.get("branches", [])
        if not val.isdigit() or int(val) >= len(names):
            raise UserError("Daftar kadaluarsa, jalankan /branches lagi.")
        set_state(uid, branch=names[int(val)])
        await safe_edit(q.message, f"✅ Branch aktif: <code>{esc(names[int(val)])}</code>")

    elif action == "ls":
        items = ctx.user_data.get("ls", [])
        if not val.isdigit() or int(val) >= len(items):
            raise UserError("Daftar kadaluarsa, jalankan /ls lagi.")
        it = items[int(val)]
        repo, branch = await resolve(uid)
        if it["type"] == "dir":
            await show_ls(update, ctx, repo, branch, it["path"])
        else:
            await send_repo_file(update, repo, branch, it["path"])

    elif action == "zip":
        mode, _, key = val.partition(":")
        p = ctx.user_data.get("pending", {}).pop(key, None)
        if not p:
            raise UserError("Permintaan kadaluarsa, kirim ulang filenya.")
        if mode == "c":
            await safe_edit(q.message, "Dibatalkan.")
            return
        await do_upload(
            ctx, q.message, p["file_id"], p["name"], p["folder"], mode, p["repo"], p["branch"]
        )

    elif action == "del":
        path = ctx.user_data.pop("del", None)
        if val != "y" or not path:
            await safe_edit(q.message, "Dibatalkan.")
            return
        repo, branch = await resolve(uid)
        await safe_edit(q.message, "⏳ Menghapus ...")
        sha, n = await run(gh.delete_path, repo, branch, path)
        await safe_edit(
            q.message,
            f"🗑 {n} file dihapus dari <code>{esc(path)}</code>\n"
            f"<a href='https://github.com/{esc(repo)}/commit/{sha}'>Lihat commit</a>",
        )

    elif action == "lk":
        link = ctx.user_data.get("link")
        if not link:
            raise UserError("Link kadaluarsa, kirim ulang.")
        repo, kind, rest = link["repo"], link["kind"], link["rest"]
        if val == "set":
            ref, _ = await resolve_ref(repo, rest if kind != "repo" else [])
            set_state(uid, repo=repo, branch=ref)
            await safe_edit(
                q.message,
                f"✅ Repo aktif: <code>{esc(repo)}</code> @ <code>{esc(ref)}</code>",
            )
        elif val == "zip":
            ref = None
            if kind in ("tree", "blob"):
                ref, _ = await resolve_ref(repo, rest)
            await send_repo_zip(update, repo, ref)
        elif val == "file":
            ref, path = await resolve_ref(repo, rest)
            await send_repo_file(update, repo, ref, path)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("help", "Bantuan"),
            BotCommand("status", "Tujuan upload saat ini"),
            BotCommand("setdir", "Set folder tujuan default"),
            BotCommand("repos", "Pilih repo"),
            BotCommand("setrepo", "Set repo aktif"),
            BotCommand("newrepo", "Buat repo baru"),
            BotCommand("branches", "Daftar/ganti branch"),
            BotCommand("setbranch", "Set branch aktif"),
            BotCommand("newbranch", "Buat branch baru"),
            BotCommand("ls", "Lihat isi repo"),
            BotCommand("get", "Download 1 file"),
            BotCommand("dl", "Download repo (zip)"),
            BotCommand("delete", "Hapus file/folder"),
        ]
    )


def main():
    if not BOT_TOKEN or not GITHUB_TOKEN:
        raise SystemExit("BOT_TOKEN dan GITHUB_TOKEN wajib diisi.")
    if not ALLOWED:
        raise SystemExit(
            "ALLOWED_USER_IDS wajib diisi (ID Telegram, pisahkan koma) agar bot tidak terbuka untuk umum."
        )
    load_state()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(120)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )
    for names, fn in [
        (("start", "help"), cmd_start),
        (("status",), cmd_status),
        (("setdir",), cmd_setdir),
        (("repos",), cmd_repos),
        (("setrepo",), cmd_setrepo),
        (("newrepo",), cmd_newrepo),
        (("branches",), cmd_branches),
        (("setbranch",), cmd_setbranch),
        (("newbranch",), cmd_newbranch),
        (("ls",), cmd_ls),
        (("get",), cmd_get),
        (("dl", "zip"), cmd_dl),
        (("delete",), cmd_delete),
    ]:
        app.add_handler(CommandHandler(names, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO,
            on_attachment,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot berjalan. User diizinkan: %s", sorted(ALLOWED))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
