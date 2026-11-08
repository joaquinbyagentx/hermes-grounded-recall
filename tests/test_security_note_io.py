"""Static links, deterministic symlink swaps and all note reread stages."""
import os
import shutil
import time
import pytest

from grounded_recall import search


def lookup(root, use_rg=False):
    return search.search_roots([root], ["orchidplan"], deadline=time.monotonic() + 3,
                               max_hits=20, use_ripgrep=use_rg)


@pytest.fixture(params=[False, True], ids=["python", "rg"])
def backend(request):
    if request.param and not shutil.which("rg"):
        pytest.skip("ripgrep unavailable")
    return request.param


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "loop", "inside"])
def test_note_symlinks_are_not_read(tmp_path, backend, kind):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("orchidplan SYNTHETIC_OUTSIDE\n")
    link = root / "linked.md"
    if kind == "directory":
        (root / "subdir").symlink_to(outside, target_is_directory=True)
    elif kind == "loop":
        link.symlink_to(link)
    elif kind == "dangling":
        link.symlink_to(outside / "missing")
    elif kind == "inside":
        (root / "target.txt").write_text("orchidplan SYNTHETIC_INSIDE_LINK\n")
        link.symlink_to(root / "target.txt")
    else:
        link.symlink_to(outside / "note.md")
    assert lookup(root, backend) == []


def test_special_files_are_not_opened_as_notes(tmp_path):
    os.mkfifo(tmp_path / "pipe.md")
    assert lookup(tmp_path) == []  # O_NONBLOCK plus fstat, no FIFO writer needed


@pytest.mark.parametrize("stage", ["scan", "weight", "label"])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_swaps_before_each_note_read_do_not_escape(tmp_path, monkeypatch, stage, kind):
    root = tmp_path / "vault"
    nested = root / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(4):  # force the weighting reread path too
        (nested / (str(i) + ".md")).write_text("orchidplan safe-%s\n" % i)
        (outside / (str(i) + ".md")).write_text("orchidplan SYNTHETIC_OUTSIDE-%s\n" % i)
    swapped = []
    def swap():
        if swapped:
            return
        swapped.append(True)
        if kind == "directory":
            nested.rename(root / "original")
            nested.symlink_to(outside, target_is_directory=True)
        else:
            for i in range(4):
                note = nested / (str(i) + ".md")
                note.unlink()
                note.symlink_to(outside / note.name)
    if stage == "scan":
        walk = search.os.walk
        def swapping_walk(*args, **kwargs):
            for row in walk(*args, **kwargs):
                if row[2]:
                    swap()  # after enumeration, before the file is opened
                yield row
        monkeypatch.setattr(search.os, "walk", swapping_walk)
    else:
        name = "_weighted" if stage == "weight" else "label_file"
        original = getattr(search, name)
        def wrapped(*args, **kwargs):
            swap()
            return original(*args, **kwargs)
        monkeypatch.setattr(search, name, wrapped)
    hits = lookup(root)
    assert swapped
    assert hits == []


def test_parent_swap_during_descriptor_walk_cannot_redirect_open(tmp_path, monkeypatch):
    from grounded_recall.note_io import NoteReader
    root = tmp_path / "vault"
    (root / "nested").mkdir(parents=True)
    (root / "nested/note.md").write_text("orchidplan SAFE_OPEN_DESCRIPTOR\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("orchidplan SYNTHETIC_OUTSIDE\n")
    original = os.open
    with NoteReader([root]) as reader:
        def racing_open(path, flags, *args, **kwargs):
            fd = original(path, flags, *args, **kwargs)
            if path == "nested":
                (root / "nested").rename(root / "moved")
                (root / "nested").symlink_to(outside, target_is_directory=True)
            return fd
        monkeypatch.setattr(os, "open", racing_open)
        with reader.open(root / "nested/note.md") as stream:
            assert stream.read() == "orchidplan SAFE_OPEN_DESCRIPTOR\n"


def test_root_is_pinned_before_enumeration(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("orchidplan SAFE_ROOT\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("orchidplan SYNTHETIC_OUTSIDE\n")
    original = search._python_scan
    def swap_root(*args, **kwargs):
        root.rename(tmp_path / "original")
        root.symlink_to(outside, target_is_directory=True)
        return original(*args, **kwargs)
    monkeypatch.setattr(search, "_python_scan", swap_root)
    hits = lookup(root)
    assert len(hits) == 1 and hits[0].text == "orchidplan SAFE_ROOT"


def test_untrusted_rg_result_paths_never_reach_weighting_or_labels(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    rows = []
    for i in range(4):
        note = outside / (str(i) + ".md")
        note.write_text("orchidplan SYNTHETIC_OUTSIDE\n")
        rows.append((note, 1))
        rows.append((root / ".." / "outside" / note.name, 1))
    monkeypatch.setattr(search, "_rg_binary", lambda _flag: "synthetic-rg")
    monkeypatch.setattr(search, "_ripgrep", lambda *_a, **_k: rows)
    assert lookup(root, True) == []


def test_missing_no_follow_support_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "note.md").write_text("orchidplan ordinary note\n")
    monkeypatch.delattr(os, "O_NOFOLLOW")
    assert lookup(tmp_path) == []


def test_relative_and_administrator_aliased_roots_still_work(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").write_text("orchidplan ordinary note\n")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert len(lookup(alias)) == 1
    monkeypatch.chdir(tmp_path)
    from pathlib import Path
    assert len(lookup(Path("vault"))) == 1


def test_descriptors_close_after_repeated_denied_reads(tmp_path):
    from grounded_recall.note_io import NoteReader
    outside = tmp_path / "outside.txt"
    outside.write_text("orchidplan SYNTHETIC_OUTSIDE\n")
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note.md").symlink_to(outside)
    with NoteReader([root]) as reader:
        fds = [fd for _, fd in reader._roots]
        for _ in range(20):
            with pytest.raises(OSError):
                with reader.open(root / "note.md"):
                    pytest.fail("symlink must not open")
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("source", ["facts", "titles"])
@pytest.mark.parametrize("race", [False, True])
def test_explicit_source_files_cannot_be_swapped_to_symlinks(tmp_path, monkeypatch, source, race):
    from grounded_recall.core import GroundedRecall
    from grounded_recall.note_io import NoteReader
    from contextlib import contextmanager
    root = tmp_path / "vault"
    root.mkdir()
    path = root / "source.md"
    outside = tmp_path / "outside.md"
    path.write_text("orchidplan ordinary content\n")
    outside.write_text("orchidplan SYNTHETIC_OUTSIDE\n")
    def swap():
        path.unlink()
        path.symlink_to(outside)
    if race:
        original = NoteReader.open
        @contextmanager
        def before_open(self, candidate):
            if candidate == path:
                swap()
            with original(self, candidate) as stream:
                yield stream
        monkeypatch.setattr(NoteReader, "open", before_open)
    else:
        swap()
    settings = {"audit_log": False, "sessions_enabled": False,
                "key_facts_file" if source == "facts" else "secret_titles_file": str(path)}
    e = GroundedRecall(lambda k, d=None: settings.get(k, d), environ={})
    if source == "facts":
        assert "SYNTHETIC_OUTSIDE" not in e.section()
    else:
        assert e.recall(user_message="orchidplan") is None
