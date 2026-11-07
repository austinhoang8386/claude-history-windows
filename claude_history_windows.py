"""Claude History Transfer for Windows — local-only Claude Desktop Code session card migration.

No external dependencies. Python 3.10+. Windows 10/11 Tkinter GUI.
Not affiliated with Anthropic or claude-transplant. No cloud or account authentication.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import dataclass
from typing import Iterable
from localization import LANGUAGE_OPTIONS, set_language, get_language, tr

APP_NAME = 'Claude History Transfer for Windows'
VERSION = '0.0.1'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
CARD_RE = re.compile(rf'^local_{UUID}\.json$', re.I)
ID_RE = re.compile(rf'^local_{UUID}$', re.I)
CLI_RE = re.compile(rf'^{UUID}$', re.I)
APP_HOME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ClaudeHistoryWindows'
JOBS_HOME = APP_HOME / 'jobs'
SETTINGS_FILE = APP_HOME / 'settings.json'


def read_saved_language(path: Path = SETTINGS_FILE) -> str:
    try:
        value = json.loads(path.read_text(encoding='utf-8')).get('language', 'en')
        return value if value in ('en', 'vi') else 'en'
    except (OSError, ValueError, AttributeError):
        return 'en'


def save_language(language: str, path: Path = SETTINGS_FILE):
    if language not in ('en', 'vi'):
        raise ValueError('Unsupported language')
    atomic_json(path, {'language': language})



class SafetyError(RuntimeError):
    pass


def filehash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Journal is always in the same directory as the temporary file.
    fd, tmpname = tempfile.mkstemp(prefix='.journal-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmpname, path)
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def detect_roots() -> list[Path]:
    """Find all supported, existing Claude Desktop Code card roots."""
    local = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
    appdata = Path(os.environ.get('APPDATA', str(Path.home())))
    roots: list[Path] = []
    # Claude MSIX installations moved to this location in October 2026.
    for path in sorted((local / 'Claude-Data').glob('claude_*/claude-code-sessions')):
        roots.append(path)
    # Original MSIX store path.
    for path in sorted((local / 'Packages').glob('Claude_*/LocalCache/Roaming/Claude/claude-code-sessions')):
        roots.append(path)
    # Standard Win32 installer path.
    roots.append(appdata / 'Claude' / 'claude-code-sessions')
    seen = set()
    result = []
    for path in roots:
        try:
            absolute = str(path.resolve()).lower()
            if absolute not in seen and path.is_dir() and not path.is_symlink():
                seen.add(absolute)
                result.append(path.resolve())
        except OSError:
            continue
    return result


@dataclass(frozen=True)
class Account:
    root: Path
    account: str
    org: str
    count: int
    latest: float

    @property
    def folder(self) -> Path:
        return self.root / self.account / self.org

    @property
    def label(self) -> str:
        return f'{self.account[:8]}… / {self.org[:8]}…    ({tr("session_count", count=self.count)})'


def accounts_at(root: Path) -> list[Account]:
    result = []
    if not root.is_dir() or root.is_symlink():
        return result
    for account in sorted(root.iterdir()):
        if not account.is_dir() or account.is_symlink():
            continue
        for org in sorted(account.iterdir()):
            if not org.is_dir() or org.is_symlink():
                continue
            cards = [f for f in org.iterdir() if f.is_file() and not f.is_symlink() and CARD_RE.fullmatch(f.name)]
            latest = max((f.stat().st_mtime for f in cards), default=org.stat().st_mtime)
            result.append(Account(root, account.name, org.name, len(cards), latest))
    return sorted(result, key=lambda a: (-a.latest, a.account, a.org))


@dataclass(frozen=True)
class Session:
    path: Path
    record_id: str
    cli_id: str
    title: str
    cwd: str
    latest: str
    valid: bool
    reason: str
    parent: str
    sha: str


def transcript_ids(base: Path) -> set[str]:
    """File-name-only lookup; does not read private transcript contents."""
    if not base.is_dir():
        return set()
    found = set()
    for file in base.glob('*/*.jsonl'):
        if file.is_file() and not file.is_symlink() and CLI_RE.fullmatch(file.stem):
            found.add(file.stem.lower())
    return found


def activity_ts(record: dict, file: Path) -> float:
    """Last activity as a Unix timestamp; falls back to the card's mtime."""
    raw = record.get('lastActivityAt') or record.get('createdAt')
    if isinstance(raw, (float, int)) and not isinstance(raw, bool):
        return raw / 1000 if raw > 1e12 else float(raw)
    if isinstance(raw, str):
        try:
            return dt.datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return file.stat().st_mtime


def date_label(record: dict, file: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(activity_ts(record, file)).strftime('%Y-%m-%d %H:%M')
    except (OverflowError, OSError, ValueError):
        return dt.datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y-%m-%d %H:%M')


def _card_activity(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        raise SafetyError(tr('dest_invalid', filename=path.name))
    data = data if isinstance(data, dict) else {}
    # Opening a session updates lastFocusedAt but not lastActivityAt, so count both.
    focused = data.get('lastFocusedAt')
    focused_ts = activity_ts({'lastActivityAt': focused}, path) if focused else 0.0
    return max(activity_ts(data, path), focused_ts)


def _replace_file(src: Path, target: Path):
    """Atomically overwrite target with src's bytes (temp file in the same folder, then os.replace)."""
    fd, tmpname = tempfile.mkstemp(prefix='.journal-', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as fw, src.open('rb') as fr:
            shutil.copyfileobj(fr, fw)
            fw.flush()
            os.fsync(fw.fileno())
        os.replace(tmpname, target)
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)


def load_sessions(account: Account, transcripts: set[str]) -> list[Session]:
    result = []
    registry = account.folder / 'scheduled-tasks.json'
    registry_text = ''
    if registry.exists():
        try:
            registry_text = registry.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            registry_text = ''  # An unreadable registry is handled as an overall safety failure below.
    for file in sorted(account.folder.glob('local_*.json')):
        if not file.is_file() or file.is_symlink():
            continue
        reason = ''
        data = {}
        if not CARD_RE.fullmatch(file.name):
            reason = tr('invalid_filename')
        else:
            try:
                data = json.loads(file.read_text(encoding='utf-8-sig'))
                if not isinstance(data, dict):
                    raise ValueError(tr('record_not_object'))
            except (OSError, ValueError, UnicodeError):
                reason = tr('invalid_json')
        rid = data.get('sessionId', '') if isinstance(data, dict) else ''
        cid = data.get('cliSessionId', '') if isinstance(data, dict) else ''
        if not reason and (rid != file.stem or not ID_RE.fullmatch(rid)):
            reason = tr('session_id_mismatch')
        if not reason and (not isinstance(cid, str) or not CLI_RE.fullmatch(cid)):
            reason = tr('invalid_cli_id')
        if not reason and cid.lower() not in transcripts:
            reason = tr('missing_transcript')
        if not reason and (data.get('scheduledTaskId') or data.get('notifySessionId')):
            reason = tr('task_or_notification')
        if not reason and registry.exists() and not registry_text:
            reason = tr('registry_unreadable')
        if not reason and registry_text and (rid in registry_text or cid in registry_text):
            reason = tr('registry_reference')
        parent = data.get('forkedFromSessionId', '') if isinstance(data, dict) else ''
        if not isinstance(parent, str):
            parent = ''
        result.append(Session(
            file, rid if isinstance(rid, str) else '', cid if isinstance(cid, str) else '',
            str(data.get('title') or tr('no_title'))[:250],
            str(data.get('cwd') or data.get('originCwd') or '')[:1000],
            date_label(data, file), not bool(reason), reason or tr('ready'), parent,
            filehash(file),
        ))
    return result


def ensure_claude_closed():
    """Fail closed when the Windows process listing cannot be verified."""
    if os.name != 'nt':
        return  # Supports offline fixture tests on other platforms; GUI is intended for Windows.
    try:
        run = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                             timeout=15, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise SafetyError(tr('process_check_failed', error=str(exc))) from exc
    names = {r[0].lower() for r in csv.reader(run.stdout.splitlines()) if r}
    # Claude.exe may be Desktop OR CLI; stopping both is deliberately conservative.
    if names.intersection({'claude.exe', 'claude desktop.exe', 'claude-code.exe'}):
        raise SafetyError(tr('close_claude'))


def _validate_dir(folder: Path, root: Path):
    if folder.is_symlink() or root.is_symlink():
        raise SafetyError(tr('symlink_unsupported'))
    if folder.parent.parent.resolve() != root.resolve():
        raise SafetyError(tr('folder_outside_root'))


def _ordered_plan(selected: Iterable[Session], source: Account, dest: Account) -> tuple[list[Session], int]:
    if source.root.resolve() != dest.root.resolve() or source.folder == dest.folder:
        raise SafetyError(tr('distinct_accounts'))
    _validate_dir(source.folder, source.root)
    _validate_dir(dest.folder, dest.root)
    sessions = list(selected)
    if not sessions:
        raise SafetyError(tr('nothing_selected'))
    by_id = {}
    for s in sessions:
        if s.path.parent.resolve() != source.folder.resolve():
            raise SafetyError(tr('session_not_in_source'))
        if not s.valid:
            raise SafetyError(tr('session_blocked', title=s.title, reason=s.reason))
        if s.path.is_symlink() or not s.path.exists() or filehash(s.path) != s.sha:
            raise SafetyError(tr('source_changed_scan'))
        if s.record_id in by_id:
            raise SafetyError(tr('duplicate_selection'))
        by_id[s.record_id] = s

    dest_ids = set()
    dest_cli = {}
    for dfile in dest.folder.glob('local_*.json'):
        if not dfile.is_file() or dfile.is_symlink():
            continue
        if not CARD_RE.fullmatch(dfile.name):
            continue
        dest_ids.add(dfile.stem)
        try:
            ddata = json.loads(dfile.read_text(encoding='utf-8-sig'))
            dcid = ddata.get('cliSessionId')
            if dcid:
                dest_cli[dcid] = dfile.name
        except (OSError, ValueError):
            raise SafetyError(tr('dest_invalid', filename=dfile.name))

    todo = {}
    skipped = 0
    pending_cli = set()
    for s in sessions:
        target = dest.folder / s.path.name
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise SafetyError(tr('same_id_conflict', filename=target.name))
            if filehash(target) == s.sha:
                skipped += 1  # No changes to either side.
                continue
            # Same session, different content: the newer card wins (account-switch sync).
            if _card_activity(s.path) <= _card_activity(target):
                raise SafetyError(tr('same_id_conflict', filename=target.name))
            if dest_cli.get(s.cli_id, target.name) != target.name or s.cli_id in pending_cli:
                raise SafetyError(tr('same_cli_conflict', cli_id=s.cli_id))
            pending_cli.add(s.cli_id)
            todo[s.record_id] = s
        else:
            if s.cli_id in dest_cli or s.cli_id in pending_cli:
                raise SafetyError(tr('same_cli_conflict', cli_id=s.cli_id))
            pending_cli.add(s.cli_id)
            todo[s.record_id] = s

    # Never move a child without the parent already being present in target or in batch.
    for s in todo.values():
        if s.parent and s.parent not in dest_ids and s.parent not in todo:
            raise SafetyError(tr('missing_fork_parent', title=s.title, parent=s.parent))

    sorted_sessions = []
    pending = dict(todo)
    ready = set(dest_ids)
    while pending:
        current = next((s for s in pending.values() if not s.parent or s.parent in ready), None)
        if not current:
            raise SafetyError(tr('fork_dependency_failed'))
        sorted_sessions.append(current)
        ready.add(current.record_id)
        del pending[current.record_id]
    return sorted_sessions, skipped


def _save_manifest(job: Path, manifest: dict):
    atomic_json(job / 'receipt.json', manifest)


def _new_job(jobs: Path) -> Path:
    jobs.mkdir(parents=True, exist_ok=True)
    job = jobs / (dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    job.mkdir(exist_ok=False)
    return job


def transfer(selected: Iterable[Session], source: Account, dest: Account, mode: str,
             jobs: Path = JOBS_HOME) -> dict:
    """Copy or move card metadata. Move is copy+verify+quarantine; transcript untouched."""
    if mode not in ('copy', 'move'):
        raise ValueError('Unknown mode')
    ensure_claude_closed()
    plan, skipped = _ordered_plan(selected, source, dest)
    if not plan:
        return {'job': None, 'done': 0, 'skipped': skipped}
    job = _new_job(jobs)
    manifest = {
        'version': 1, 'mode': mode, 'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'root': str(source.root), 'source': str(source.folder), 'dest': str(dest.folder),
        'state': 'running', 'skipped': skipped,
        'items': [
            {'filename': s.path.name, 'sha256': s.sha, 'status': 'planned'} for s in plan
        ],
    }
    for item in manifest['items']:
        existing = dest.folder / item['filename']
        if existing.exists():
            item['replaced_sha256'] = filehash(existing)  # Older destination card being overwritten.
    _save_manifest(job, manifest)
    try:
        # Backup ALL chosen cards before writing to the target.
        (job / 'backup-source').mkdir()
        for s in plan:
            backup = job / 'backup-source' / s.path.name
            shutil.copy2(s.path, backup)
            if filehash(backup) != s.sha:
                raise SafetyError(tr('backup_hash_mismatch', filename=s.path.name))
        (job / 'backup-dest').mkdir()
        for item in manifest['items']:
            if 'replaced_sha256' in item:
                backup = job / 'backup-dest' / item['filename']
                shutil.copy2(dest.folder / item['filename'], backup)
                if filehash(backup) != item['replaced_sha256']:
                    raise SafetyError(tr('backup_hash_mismatch', filename=item['filename']))
        manifest['state'] = 'backed_up'
        _save_manifest(job, manifest)
        for i, s in enumerate(plan):
            target = dest.folder / s.path.name
            replaced = manifest['items'][i].get('replaced_sha256')
            if target.exists() and (not replaced or filehash(target) != replaced):
                raise SafetyError(tr('dest_appeared', filename=target.name))
            if filehash(s.path) != s.sha:
                raise SafetyError(tr('source_changed_transfer'))
            if replaced:
                _replace_file(s.path, target)  # Old card is verified in backup-dest for undo.
            else:
                # Exclusive creation prevents accidental overwrite and makes rollback verifiable.
                with s.path.open('rb') as fr, target.open('xb') as fw:
                    shutil.copyfileobj(fr, fw)
                    fw.flush()
                    os.fsync(fw.fileno())
            if filehash(target) != s.sha:
                raise SafetyError(tr('target_hash_mismatch'))
            manifest['items'][i]['status'] = 'copied'
            _save_manifest(job, manifest)
            if mode == 'move':
                # Keep an independently verified quarantined copy before removing source.
                if filehash(job / 'backup-source' / s.path.name) != s.sha or filehash(s.path) != s.sha:
                    raise SafetyError(tr('source_or_backup_changed'))
                s.path.unlink()
                manifest['items'][i]['status'] = 'moved'
                _save_manifest(job, manifest)
        manifest['state'] = 'complete'
        _save_manifest(job, manifest)
    except Exception as exc:
        manifest['state'] = 'interrupted'
        manifest['error'] = str(exc)
        _save_manifest(job, manifest)
        raise SafetyError(tr('interrupted', error=exc, job=job)) from exc
    return {'job': job, 'done': len(plan), 'skipped': skipped}


def undo_job(job: Path) -> dict:
    ensure_claude_closed()
    manifest_file = job / 'receipt.json'
    if not manifest_file.is_file():
        raise SafetyError(tr('receipt_missing'))
    receipt = json.loads(manifest_file.read_text(encoding='utf-8'))
    if receipt.get('version') != 1 or receipt.get('mode') not in ('copy', 'move'):
        raise SafetyError(tr('unsupported_job'))
    root = Path(receipt['root'])
    src = Path(receipt['source'])
    dst = Path(receipt['dest'])
    _validate_dir(src, root)
    _validate_dir(dst, root)
    if not src.is_dir() or not dst.is_dir():
        raise SafetyError(tr('folder_missing'))
    removed = 0
    restored = 0
    errors = []
    for entry in reversed(receipt['items']):
        filename, sha, status = entry['filename'], entry['sha256'], entry['status']
        if not CARD_RE.fullmatch(filename):
            errors.append(tr('invalid_receipt_filename')); continue
        if status == 'undone':
            continue
        original = src / filename
        target = dst / filename
        backup = job / 'backup-source' / filename
        replaced = entry.get('replaced_sha256')
        try:
            # Planned records may have been copied just before a crash; inspect the target.
            if backup.exists() and filehash(backup) != sha:
                raise SafetyError(tr('backup_changed'))
            if original.exists() and (original.is_symlink() or filehash(original) != sha):
                raise SafetyError(tr('source_edited_undo'))
            target_sha = filehash(target) if target.exists() and not target.is_symlink() else None
            if target.exists() and target_sha not in (sha, replaced):
                raise SafetyError(tr('target_edited_undo'))
            if receipt['mode'] == 'move' and not original.exists():
                if not backup.exists():
                    raise SafetyError(tr('backup_absent'))
                with backup.open('rb') as fr, original.open('xb') as fw:
                    shutil.copyfileobj(fr, fw)
                    fw.flush()
                    os.fsync(fw.fileno())
                if filehash(original) != sha:
                    raise SafetyError(tr('restored_hash_bad'))
                restored += 1
            if replaced and target_sha == sha:
                old = job / 'backup-dest' / filename
                if not old.exists() or filehash(old) != replaced:
                    raise SafetyError(tr('backup_absent'))
                _replace_file(old, target)  # Put the older destination card back.
                removed += 1
            elif not replaced and target.exists():
                target.unlink()
                removed += 1
            entry['status'] = 'undone'
            _save_manifest(job, receipt)
        except (OSError, SafetyError) as exc:
            errors.append(f'{filename}: {exc}')
    receipt['state'] = 'undo_partial' if errors else 'undone'
    receipt['undo_errors'] = errors
    _save_manifest(job, receipt)
    return {'removed': removed, 'restored': restored, 'errors': errors, 'job': job}


def last_undoable_job(jobs: Path = JOBS_HOME) -> Path | None:
    if not jobs.is_dir():
        return None
    for job in sorted(jobs.iterdir(), reverse=True):
        receipt = job / 'receipt.json'
        if not receipt.is_file():
            continue
        try:
            data = json.loads(receipt.read_text(encoding='utf-8'))
            if any(i.get('status') != 'undone' for i in data.get('items', [])):
                return job
        except (OSError, ValueError):
            continue
    return None


class TransferGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        set_language(read_saved_language(SETTINGS_FILE))
        self.title(APP_NAME + '  ' + VERSION)
        self.geometry('1150x730')
        self.minsize(950, 575)
        self.configure(bg='#f5f6f9')
        style = ttk.Style(self)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        self.roots: list[Path] = []
        self.accts: list[Account] = []
        self.sessions: list[Session] = []
        self.checked: set[str] = set()
        self.localized_widgets: list[tuple[tk.Widget, str]] = []
        self.root_text = tk.StringVar()
        self.src_text = tk.StringVar()
        self.dst_text = tk.StringVar()
        self.filter_text = tk.StringVar()
        self.status_text = tk.StringVar(value=tr('ready'))
        self.info_text = tk.StringVar()
        self.language_text = tk.StringVar(value='Tiếng Việt' if get_language() == 'vi' else 'English')
        self.build_ui()
        self.refresh_roots()

    def localize(self, widget, key: str):
        widget.configure(text=tr(key))
        self.localized_widgets.append((widget, key))
        return widget

    def change_language(self, _event=None):
        language = LANGUAGE_OPTIONS.get(self.language_text.get(), 'en')
        if language == get_language():
            return
        set_language(language)
        try:
            save_language(language, SETTINGS_FILE)
        except OSError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
        for widget, key in self.localized_widgets:
            widget.configure(text=tr(key))
        for key in ('mark', 'title', 'project', 'date', 'status'):
            self.table.heading(key, text=tr('column_' + key))
        # Preserve index because changing Combobox values may clear the selection.
        old_src, old_dst = self.src_box.current(), self.dst_box.current()
        self.src_box['values'] = [a.label for a in self.accts]
        self.dst_box['values'] = [a.label for a in self.accts]
        if 0 <= old_src < len(self.accts):
            self.src_box.current(old_src)
        if 0 <= old_dst < len(self.accts):
            self.dst_box.current(old_dst)
        src, _ = self.current_accounts()
        if src:
            ids = transcript_ids(Path.home() / '.claude' / 'projects')
            self.sessions = load_sessions(src, ids)
            self.sessions.sort(key=lambda s: s.latest, reverse=True)
        self.populate_table()  # Preserve checkboxes and filter while translating reasons.
        self.status_text.set(tr('ready'))

    def build_ui(self):
        header = tk.Frame(self, bg='#25314b', height=76)
        header.pack(fill='x')
        title_area = tk.Frame(header, bg='#25314b')
        title_area.pack(side='left', fill='x', expand=True)
        tk.Label(title_area, text='CLAUDE HISTORY TRANSFER', fg='white', bg='#25314b',
                 font=('Segoe UI', 17, 'bold')).pack(anchor='w', padx=22, pady=(12, 0))
        self.localize(tk.Label(title_area, fg='#c9d7eb', bg='#25314b',
                               font=('Segoe UI', 9)), 'app_subtitle').pack(anchor='w', padx=23)
        lang_area = tk.Frame(header, bg='#25314b')
        lang_area.pack(side='right', padx=20, pady=19)
        self.localize(tk.Label(lang_area, fg='#c9d7eb', bg='#25314b'), 'language').pack(side='left', padx=(0, 6))
        lang_box = ttk.Combobox(lang_area, values=list(LANGUAGE_OPTIONS),
                                textvariable=self.language_text, state='readonly', width=13)
        lang_box.pack(side='left')
        lang_box.bind('<<ComboboxSelected>>', self.change_language)

        body = ttk.Frame(self, padding=(18, 14))
        body.pack(fill='both', expand=True)

        location = self.localize(ttk.LabelFrame(body, padding=10), 'root_group')
        location.pack(fill='x', pady=(0, 10))
        self.root_box = ttk.Combobox(location, textvariable=self.root_text, state='readonly')
        self.root_box.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.root_box.bind('<<ComboboxSelected>>', lambda _: self.refresh_accounts())
        self.localize(ttk.Button(location, command=self.pick_root), 'browse').pack(side='left', padx=3)
        self.localize(ttk.Button(location, command=self.refresh_roots), 'rescan').pack(side='left', padx=3)

        account = self.localize(ttk.LabelFrame(body, padding=10), 'accounts_group')
        account.pack(fill='x', pady=(0, 10))
        account.columnconfigure(1, weight=1)
        account.columnconfigure(3, weight=1)
        ttk.Label(account, text='FROM').grid(row=0, column=0, sticky='w', padx=(0, 8))
        self.src_box = ttk.Combobox(account, textvariable=self.src_text, state='readonly')
        self.src_box.grid(row=0, column=1, sticky='ew', padx=(0, 16))
        self.src_box.bind('<<ComboboxSelected>>', lambda _: self.refresh_sessions())
        ttk.Label(account, text='TO').grid(row=0, column=2, sticky='w', padx=(0, 8))
        self.dst_box = ttk.Combobox(account, textvariable=self.dst_text, state='readonly')
        self.dst_box.grid(row=0, column=3, sticky='ew')
        self.localize(ttk.Label(account, foreground='#606778'), 'account_help').grid(
            row=1, column=0, columnspan=4, sticky='w', pady=(7, 0))

        listing = self.localize(ttk.LabelFrame(body, padding=10), 'sessions_group')
        listing.pack(fill='both', expand=True)
        toolbar = ttk.Frame(listing)
        toolbar.pack(fill='x', pady=(0, 8))
        self.localize(ttk.Label(toolbar), 'search').pack(side='left')
        query = ttk.Entry(toolbar, textvariable=self.filter_text, width=35)
        query.pack(side='left', fill='x', expand=True, padx=9)
        self.filter_text.trace_add('write', lambda *_: self.populate_table())
        self.localize(ttk.Button(toolbar, command=self.select_all), 'select_all').pack(side='left', padx=4)
        self.localize(ttk.Button(toolbar, command=self.deselect_all), 'deselect').pack(side='left', padx=4)

        tree_frame = ttk.Frame(listing)
        tree_frame.pack(fill='both', expand=True)
        cols = ('mark', 'title', 'project', 'date', 'status')
        self.table = ttk.Treeview(tree_frame, columns=cols, show='headings', selectmode='browse')
        for key in cols:
            self.table.heading(key, text=tr('column_' + key))
        for key, size, stretch in [('mark', 45, False), ('title', 280, True), ('project', 280, True),
                                   ('date', 140, False), ('status', 180, False)]:
            self.table.column(key, width=size, minwidth=35, stretch=stretch)
        self.table.tag_configure('blocked', foreground='#aa4e4e')
        ys = ttk.Scrollbar(tree_frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=ys.set)
        self.table.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')
        self.table.bind('<ButtonRelease-1>', self.on_row_click)
        self.table.bind('<Double-1>', self.on_row_click)
        ttk.Label(listing, textvariable=self.info_text, foreground='#566071').pack(anchor='w', pady=(7, 0))

        footer = ttk.Frame(body)
        footer.pack(fill='x', pady=(11, 0))
        self.localize(ttk.Button(footer, command=lambda: self.execute('copy')), 'copy_button').pack(side='left', padx=(0, 8))
        self.localize(ttk.Button(footer, command=lambda: self.execute('move')), 'move_button').pack(side='left', padx=(0, 8))
        self.localize(ttk.Button(footer, command=self.undo), 'undo_button').pack(side='left')
        ttk.Label(footer, textvariable=self.status_text, foreground='#4c5d73').pack(side='right')
        self.localize(ttk.Label(body, foreground='#8d4a35'), 'scope_notice').pack(anchor='w', pady=(9, 0))

    def current_root(self) -> Path | None:
        idx = self.root_box.current()
        return self.roots[idx] if 0 <= idx < len(self.roots) else None

    def current_accounts(self) -> tuple[Account | None, Account | None]:
        a, b = self.src_box.current(), self.dst_box.current()
        return (self.accts[a] if 0 <= a < len(self.accts) else None,
                self.accts[b] if 0 <= b < len(self.accts) else None)

    def pick_root(self):
        folder = filedialog.askdirectory(title=tr('browse_title'))
        if not folder:
            return
        path = Path(folder).resolve()
        if path.name != 'claude-code-sessions':
            messagebox.showerror(tr('wrong_folder_title'), tr('wrong_folder'))
            return
        if path not in self.roots:
            self.roots.append(path)
        self.root_box['values'] = [str(p) for p in self.roots]
        self.root_box.current(self.roots.index(path))
        self.refresh_accounts()

    def refresh_roots(self):
        old = self.current_root()
        custom = [r for r in self.roots if r.is_dir()]
        self.roots = detect_roots()
        for r in custom:
            if r not in self.roots:
                self.roots.append(r)
        self.root_box['values'] = [str(p) for p in self.roots]
        if not self.roots:
            self.status_text.set(tr('no_root_status'))
            self.accts = []
            self.sessions = []
            self.populate_table()
            messagebox.showinfo(tr('no_root_title'), tr('no_root_message'))
            return
        self.root_box.current(self.roots.index(old) if old in self.roots else 0)
        self.refresh_accounts()

    def refresh_accounts(self):
        root = self.current_root()
        self.accts = accounts_at(root) if root else []
        labels = [a.label for a in self.accts]
        self.src_box['values'] = labels
        self.dst_box['values'] = labels
        if self.accts:
            self.src_box.current(next((i for i,a in enumerate(self.accts) if a.count), 0))
            self.dst_box.current(next((i for i,a in enumerate(self.accts) if i != self.src_box.current()), 0))
        else:
            self.src_text.set(''); self.dst_text.set('')
        self.refresh_sessions()

    def refresh_sessions(self):
        self.checked.clear()
        src, _ = self.current_accounts()
        if not src:
            self.sessions = []
            self.populate_table()
            return
        self.status_text.set(tr('scanning'))
        self.update_idletasks()
        try:
            ids = transcript_ids(Path.home() / '.claude' / 'projects')
            self.sessions = load_sessions(src, ids)
            self.sessions.sort(key=lambda s: s.latest, reverse=True)
        except Exception as exc:
            messagebox.showerror(tr('scan_error'), str(exc))
            self.sessions = []
        self.populate_table()
        self.status_text.set(tr('scanned'))

    def visible_sessions(self) -> list[Session]:
        q = self.filter_text.get().lower().strip()
        return [s for s in self.sessions if not q or q in s.title.lower() or q in s.cwd.lower()]

    def populate_table(self):
        self.table.delete(*self.table.get_children())
        for i, s in enumerate(self.visible_sessions()):
            self.table.insert('', 'end', iid=str(i), values=(
                '☑' if str(s.path) in self.checked else '☐', s.title,
                s.cwd, s.latest, s.reason), tags=() if s.valid else ('blocked',))
        count = sum(str(s.path) in self.checked for s in self.sessions)
        ready = sum(s.valid for s in self.sessions)
        self.info_text.set(tr('table_summary', total=len(self.sessions), eligible=ready, selected=count))

    def on_row_click(self, event):
        row = self.table.identify_row(event.y)
        col = self.table.identify_column(event.x)
        if row and col == '#1':
            selected = self.visible_sessions()[int(row)]
            if selected.valid:
                key = str(selected.path)
                if key in self.checked:
                    self.checked.remove(key)
                else:
                    self.checked.add(key)
                self.populate_table()

    def select_all(self):
        self.checked.update(str(s.path) for s in self.visible_sessions() if s.valid)
        self.populate_table()

    def deselect_all(self):
        self.checked.clear()
        self.populate_table()

    def execute(self, mode: str):
        src, dst = self.current_accounts()
        if not src or not dst:
            messagebox.showwarning(tr('account_missing_title'), tr('account_missing'))
            return
        selected = [s for s in self.sessions if str(s.path) in self.checked]
        try:
            ensure_claude_closed()
            plan, skipped = _ordered_plan(selected, src, dst)
        except SafetyError as exc:
            messagebox.showerror(tr('unsafe_title'), str(exc))
            return
        verb = tr('copy_verb') if mode == 'copy' else tr('move_verb')
        message = tr('confirmation', verb=verb, count=len(plan), source=src.folder,
                     dest=dst.folder, skipped=skipped)
        if mode == 'move':
            message += tr('move_warning')
        if not plan:
            messagebox.showinfo(tr('nothing_to_do_title'), tr('nothing_to_do'))
            return
        if not messagebox.askyesno(tr('confirm_title', verb=verb), message):
            return
        try:
            result = transfer(selected, src, dst, mode)
            self.status_text.set(tr('complete_status', count=result['done']))
            messagebox.showinfo(tr('complete_title'), tr('completed_message',
                                verb=verb, done=result['done'], skipped=result['skipped'], job=result['job']))
            self.refresh_accounts()
        except (OSError, SafetyError) as exc:
            messagebox.showerror(tr('stopped'), str(exc))
            self.refresh_accounts()

    def undo(self):
        job = last_undoable_job()
        if not job:
            messagebox.showinfo('Undo', tr('undo_none'))
            return
        if not messagebox.askyesno(tr('confirm_undo'), tr('undo_confirmation', job=job)):
            return
        try:
            result = undo_job(job)
            messagebox.showinfo('Undo', tr('undo_result', removed=result['removed'],
                                restored=result['restored'], errors=len(result['errors']),
                                details='\n'.join(result['errors'][:5])))
            self.refresh_accounts()
        except (SafetyError, OSError, ValueError) as exc:
            messagebox.showerror(tr('undo_failed'), str(exc))


def main():
    if '--version' in sys.argv:
        print(VERSION)
        return
    app = TransferGUI()
    app.mainloop()


if __name__ == '__main__':
    main()
