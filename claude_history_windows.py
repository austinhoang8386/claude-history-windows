"""Claude History Transfer for Windows — local-only Claude Desktop Code session card migration.

Python 3.10+ and wxPython (native, screen-reader accessible controls). Windows 10/11.
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
import wx
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


def date_label(record: dict, file: Path) -> str:
    raw = record.get('lastActivityAt') or record.get('createdAt')
    if isinstance(raw, (float, int)):
        try:
            if raw > 1e12:
                raw /= 1000
            return dt.datetime.fromtimestamp(raw).strftime('%Y-%m-%d %H:%M')
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(raw, str):
        try:
            return dt.datetime.fromisoformat(raw.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y-%m-%d %H:%M')


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
            if not target.is_file() or target.is_symlink() or filehash(target) != s.sha:
                raise SafetyError(tr('same_id_conflict', filename=target.name))
            skipped += 1  # No changes to either side.
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
    _save_manifest(job, manifest)
    try:
        # Backup ALL chosen cards before writing to the target.
        (job / 'backup-source').mkdir()
        for s in plan:
            backup = job / 'backup-source' / s.path.name
            shutil.copy2(s.path, backup)
            if filehash(backup) != s.sha:
                raise SafetyError(tr('backup_hash_mismatch', filename=s.path.name))
        manifest['state'] = 'backed_up'
        _save_manifest(job, manifest)
        for i, s in enumerate(plan):
            target = dest.folder / s.path.name
            if target.exists():
                raise SafetyError(tr('dest_appeared', filename=target.name))
            if filehash(s.path) != s.sha:
                raise SafetyError(tr('source_changed_transfer'))
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
        try:
            # Planned records may have been copied just before a crash; inspect the target.
            if backup.exists() and filehash(backup) != sha:
                raise SafetyError(tr('backup_changed'))
            if original.exists() and (original.is_symlink() or filehash(original) != sha):
                raise SafetyError(tr('source_edited_undo'))
            if target.exists() and (target.is_symlink() or filehash(target) != sha):
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
            if target.exists():
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


class TransferGUI(wx.Frame):
    """wxPython GUI: native Win32 controls, so NVDA/JAWS/Narrator can read and drive every widget."""

    def __init__(self):
        set_language(read_saved_language(SETTINGS_FILE))
        super().__init__(None, title=APP_NAME + '  ' + VERSION, size=(1150, 730))
        self.SetMinSize((950, 575))
        self.roots: list[Path] = []
        self.accts: list[Account] = []
        self.sessions: list[Session] = []
        self.shown: list[Session] = []
        self.checked: set[str] = set()
        self.localized_widgets: list[tuple[wx.Window, str]] = []
        self._populating = False
        self.build_ui()
        self.refresh_roots()

    def localize(self, widget, key: str):
        widget.SetLabel(tr(key))
        self.localized_widgets.append((widget, key))
        return widget

    def show(self, message: str, title: str, flags=wx.OK | wx.ICON_INFORMATION) -> int:
        return wx.MessageBox(message, title, flags, self)

    def change_language(self, _event=None):
        language = list(LANGUAGE_OPTIONS.values())[self.lang_box.GetSelection()]
        if language == get_language():
            return
        set_language(language)
        try:
            save_language(language, SETTINGS_FILE)
        except OSError as exc:
            self.show(str(exc), APP_NAME, wx.OK | wx.ICON_WARNING)
        for widget, key in self.localized_widgets:
            widget.SetLabel(tr(key))
        for i, key in enumerate(self.columns):
            col = self.table.GetColumn(i)
            col.SetText(tr('column_' + key))
            self.table.SetColumn(i, col)
        old_src, old_dst = self.src_box.GetSelection(), self.dst_box.GetSelection()
        self.set_account_choices()
        if 0 <= old_src < len(self.accts):
            self.src_box.SetSelection(old_src)
        if 0 <= old_dst < len(self.accts):
            self.dst_box.SetSelection(old_dst)
        src, _ = self.current_accounts()
        if src:
            ids = transcript_ids(Path.home() / '.claude' / 'projects')
            self.sessions = load_sessions(src, ids)
            self.sessions.sort(key=lambda s: s.latest, reverse=True)
        self.populate_table()  # Preserve checkboxes and filter while translating reasons.
        self.status.SetLabel(tr('ready'))
        self.panel.Layout()

    def build_ui(self):
        self.panel = panel = wx.Panel(self)  # wx.Panel gives Tab/Shift+Tab keyboard traversal.
        outer = wx.BoxSizer(wx.VERTICAL)

        header = wx.Panel(panel)
        header.SetBackgroundColour('#25314b')
        hs = wx.BoxSizer(wx.HORIZONTAL)
        titles = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(header, label='CLAUDE HISTORY TRANSFER')
        title.SetFont(wx.Font(17, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD, faceName='Segoe UI'))
        title.SetForegroundColour('white')
        subtitle = self.localize(wx.StaticText(header), 'app_subtitle')
        subtitle.SetForegroundColour('#c9d7eb')
        titles.Add(title, 0, wx.LEFT | wx.TOP, 12)
        titles.Add(subtitle, 0, wx.LEFT | wx.BOTTOM, 12)
        hs.Add(titles, 1, wx.EXPAND)
        # Each StaticText is created right before its control: Windows uses it as the control's accessible name.
        lang_label = self.localize(wx.StaticText(header), 'language')
        lang_label.SetForegroundColour('#c9d7eb')
        self.lang_box = wx.Choice(header, choices=list(LANGUAGE_OPTIONS))
        self.lang_box.SetSelection(list(LANGUAGE_OPTIONS.values()).index(get_language()))
        self.lang_box.Bind(wx.EVT_CHOICE, self.change_language)
        hs.Add(lang_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        hs.Add(self.lang_box, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        header.SetSizer(hs)
        outer.Add(header, 0, wx.EXPAND)

        body = wx.BoxSizer(wx.VERTICAL)

        # Controls are children of their StaticBox so screen readers announce the group name.
        location = wx.StaticBoxSizer(wx.HORIZONTAL, panel)
        box = self.localize(location.GetStaticBox(), 'root_group')
        self.root_box = wx.Choice(box)
        self.root_box.Bind(wx.EVT_CHOICE, lambda _: self.refresh_accounts())
        browse = self.localize(wx.Button(box), 'browse')
        browse.Bind(wx.EVT_BUTTON, lambda _: self.pick_root())
        rescan = self.localize(wx.Button(box), 'rescan')
        rescan.Bind(wx.EVT_BUTTON, lambda _: self.refresh_roots())
        location.Add(self.root_box, 1, wx.EXPAND | wx.ALL, 6)
        location.Add(browse, 0, wx.ALL, 6)
        location.Add(rescan, 0, wx.ALL, 6)
        body.Add(location, 0, wx.EXPAND | wx.BOTTOM, 10)

        account = wx.StaticBoxSizer(wx.VERTICAL, panel)
        box = self.localize(account.GetStaticBox(), 'accounts_group')
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(box, label='FROM'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.src_box = wx.Choice(box)
        self.src_box.Bind(wx.EVT_CHOICE, lambda _: self.refresh_sessions())
        row.Add(self.src_box, 1, wx.RIGHT, 16)
        row.Add(wx.StaticText(box, label='TO'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.dst_box = wx.Choice(box)
        row.Add(self.dst_box, 1)
        account.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        account_help = self.localize(wx.StaticText(box), 'account_help')
        account_help.SetForegroundColour('#4a5160')
        account.Add(account_help, 0, wx.ALL, 6)
        body.Add(account, 0, wx.EXPAND | wx.BOTTOM, 10)

        listing = wx.StaticBoxSizer(wx.VERTICAL, panel)
        box = self.localize(listing.GetStaticBox(), 'sessions_group')
        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        toolbar.Add(self.localize(wx.StaticText(box), 'search'), 0, wx.ALIGN_CENTER_VERTICAL)
        self.query = wx.TextCtrl(box)
        self.query.Bind(wx.EVT_TEXT, lambda _: self.populate_table())
        toolbar.Add(self.query, 1, wx.LEFT | wx.RIGHT, 9)
        select_all = self.localize(wx.Button(box), 'select_all')
        select_all.Bind(wx.EVT_BUTTON, lambda _: self.select_all())
        deselect = self.localize(wx.Button(box), 'deselect')
        deselect.Bind(wx.EVT_BUTTON, lambda _: self.deselect_all())
        toolbar.Add(select_all, 0, wx.LEFT | wx.RIGHT, 4)
        toolbar.Add(deselect, 0, wx.LEFT, 4)
        listing.Add(toolbar, 0, wx.EXPAND | wx.ALL, 6)

        # Native list view with checkboxes: Space toggles, screen readers announce "checked"/"not checked".
        # Its label sits right before it so the list gets an accessible name.
        listing.Add(self.localize(wx.StaticText(box), 'sessions_group'), 0, wx.LEFT, 6)
        self.table = wx.ListCtrl(box, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.table.EnableCheckBoxes()
        self.columns = ('title', 'project', 'date', 'status')
        for i, (key, size) in enumerate(zip(self.columns, (300, 300, 140, 260))):
            self.table.InsertColumn(i, tr('column_' + key), width=size)
        self.table.Bind(wx.EVT_LIST_ITEM_CHECKED, self.on_check)
        self.table.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self.on_check)
        listing.Add(self.table, 1, wx.EXPAND | wx.ALL, 6)
        self.info = wx.StaticText(box)
        listing.Add(self.info, 0, wx.ALL, 6)
        body.Add(listing, 1, wx.EXPAND)

        footer = wx.BoxSizer(wx.HORIZONTAL)
        for key, handler in (('copy_button', lambda _: self.execute('copy')),
                             ('move_button', lambda _: self.execute('move')),
                             ('undo_button', lambda _: self.undo())):
            button = self.localize(wx.Button(panel), key)
            button.Bind(wx.EVT_BUTTON, handler)
            footer.Add(button, 0, wx.RIGHT, 8)
        footer.AddStretchSpacer()
        self.status = wx.StaticText(panel, label=tr('ready'))
        footer.Add(self.status, 0, wx.ALIGN_CENTER_VERTICAL)
        body.Add(footer, 0, wx.EXPAND | wx.TOP, 11)
        notice = self.localize(wx.StaticText(panel), 'scope_notice')
        notice.SetForegroundColour('#8d4a35')
        body.Add(notice, 0, wx.TOP, 9)

        outer.Add(body, 1, wx.EXPAND | wx.ALL, 16)
        panel.SetSizer(outer)

    def set_account_choices(self):
        labels = [a.label for a in self.accts]
        self.src_box.Set(labels)
        self.dst_box.Set(labels)

    def current_root(self) -> Path | None:
        idx = self.root_box.GetSelection()
        return self.roots[idx] if 0 <= idx < len(self.roots) else None

    def current_accounts(self) -> tuple[Account | None, Account | None]:
        a, b = self.src_box.GetSelection(), self.dst_box.GetSelection()
        return (self.accts[a] if 0 <= a < len(self.accts) else None,
                self.accts[b] if 0 <= b < len(self.accts) else None)

    def pick_root(self):
        with wx.DirDialog(self, tr('browse_title')) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath()).resolve()
        if path.name != 'claude-code-sessions':
            self.show(tr('wrong_folder'), tr('wrong_folder_title'), wx.OK | wx.ICON_ERROR)
            return
        if path not in self.roots:
            self.roots.append(path)
        self.root_box.Set([str(p) for p in self.roots])
        self.root_box.SetSelection(self.roots.index(path))
        self.refresh_accounts()

    def refresh_roots(self):
        old = self.current_root()
        custom = [r for r in self.roots if r.is_dir()]
        self.roots = detect_roots()
        for r in custom:
            if r not in self.roots:
                self.roots.append(r)
        self.root_box.Set([str(p) for p in self.roots])
        if not self.roots:
            self.status.SetLabel(tr('no_root_status'))
            self.accts = []
            self.sessions = []
            self.set_account_choices()
            self.populate_table()
            self.show(tr('no_root_message'), tr('no_root_title'))
            return
        self.root_box.SetSelection(self.roots.index(old) if old in self.roots else 0)
        self.refresh_accounts()

    def refresh_accounts(self):
        root = self.current_root()
        self.accts = accounts_at(root) if root else []
        self.set_account_choices()
        if self.accts:
            self.src_box.SetSelection(next((i for i, a in enumerate(self.accts) if a.count), 0))
            self.dst_box.SetSelection(next((i for i, a in enumerate(self.accts) if i != self.src_box.GetSelection()), 0))
        self.refresh_sessions()

    def refresh_sessions(self):
        self.checked.clear()
        src, _ = self.current_accounts()
        if not src:
            self.sessions = []
            self.populate_table()
            return
        self.status.SetLabel(tr('scanning'))
        self.Update()
        try:
            ids = transcript_ids(Path.home() / '.claude' / 'projects')
            self.sessions = load_sessions(src, ids)
            self.sessions.sort(key=lambda s: s.latest, reverse=True)
        except Exception as exc:
            self.show(str(exc), tr('scan_error'), wx.OK | wx.ICON_ERROR)
            self.sessions = []
        self.populate_table()
        self.status.SetLabel(tr('scanned'))

    def visible_sessions(self) -> list[Session]:
        q = self.query.GetValue().lower().strip()
        return [s for s in self.sessions if not q or q in s.title.lower() or q in s.cwd.lower()]

    def update_summary(self):
        count = sum(str(s.path) in self.checked for s in self.sessions)
        ready = sum(s.valid for s in self.sessions)
        self.info.SetLabel(tr('table_summary', total=len(self.sessions), eligible=ready, selected=count))

    def populate_table(self):
        self._populating = True  # CheckItem fires check events; ignore our own.
        try:
            self.table.DeleteAllItems()
            self.shown = self.visible_sessions()
            for i, s in enumerate(self.shown):
                self.table.InsertItem(i, s.title)
                for col, value in enumerate((s.cwd, s.latest, s.reason), start=1):
                    self.table.SetItem(i, col, value)
                if not s.valid:
                    self.table.SetItemTextColour(i, wx.Colour('#aa4e4e'))  # Status column also states why.
                self.table.CheckItem(i, str(s.path) in self.checked)
        finally:
            self._populating = False
        self.update_summary()

    def on_check(self, event):
        if self._populating:
            return
        i = event.GetIndex()
        s = self.shown[i]
        if not s.valid:
            # Blocked rows cannot be selected; revert the tick after the native toggle finishes.
            wx.Bell()
            wx.CallAfter(self.populate_table)
            return
        if self.table.IsItemChecked(i):
            self.checked.add(str(s.path))
        else:
            self.checked.discard(str(s.path))
        self.update_summary()

    def select_all(self):
        self.checked.update(str(s.path) for s in self.visible_sessions() if s.valid)
        self.populate_table()

    def deselect_all(self):
        self.checked.clear()
        self.populate_table()

    def execute(self, mode: str):
        src, dst = self.current_accounts()
        if not src or not dst:
            self.show(tr('account_missing'), tr('account_missing_title'), wx.OK | wx.ICON_WARNING)
            return
        selected = [s for s in self.sessions if str(s.path) in self.checked]
        try:
            ensure_claude_closed()
            plan, skipped = _ordered_plan(selected, src, dst)
        except SafetyError as exc:
            self.show(str(exc), tr('unsafe_title'), wx.OK | wx.ICON_ERROR)
            return
        verb = tr('copy_verb') if mode == 'copy' else tr('move_verb')
        message = tr('confirmation', verb=verb, count=len(plan), source=src.folder,
                     dest=dst.folder, skipped=skipped)
        if mode == 'move':
            message += tr('move_warning')
        if not plan:
            self.show(tr('nothing_to_do'), tr('nothing_to_do_title'))
            return
        if self.show(message, tr('confirm_title', verb=verb),
                     wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) != wx.YES:
            return
        try:
            result = transfer(selected, src, dst, mode)
            self.status.SetLabel(tr('complete_status', count=result['done']))
            self.show(tr('completed_message', verb=verb, done=result['done'],
                         skipped=result['skipped'], job=result['job']), tr('complete_title'))
            self.refresh_accounts()
        except (OSError, SafetyError) as exc:
            self.show(str(exc), tr('stopped'), wx.OK | wx.ICON_ERROR)
            self.refresh_accounts()

    def undo(self):
        job = last_undoable_job()
        if not job:
            self.show(tr('undo_none'), 'Undo')
            return
        if self.show(tr('undo_confirmation', job=job), tr('confirm_undo'),
                     wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) != wx.YES:
            return
        try:
            result = undo_job(job)
            self.show(tr('undo_result', removed=result['removed'], restored=result['restored'],
                         errors=len(result['errors']), details='\n'.join(result['errors'][:5])), 'Undo')
            self.refresh_accounts()
        except (SafetyError, OSError, ValueError) as exc:
            self.show(str(exc), tr('undo_failed'), wx.OK | wx.ICON_ERROR)


def main():
    if '--version' in sys.argv:
        print(VERSION)
        return
    app = wx.App(False)
    TransferGUI().Show()
    app.MainLoop()


if __name__ == '__main__':
    main()
