#!/usr/bin/env python3
"""
Offline test environment for the live CADC / Datatrail path.

The real `cadc-datatrail` source is what we cannot exercise without CANFAR: its
enumerate() reads an inventory.jsonl, and its fetch() pulls each file with
cadcget (with retry + backoff). This test drives that REAL source code
end-to-end with NO CADC access, by injecting a fake StorageInventoryClient that
serves synthetic baseband files from a local fixture directory, then reducing
them with the spectrum analyzer.

It validates the integration glue that otherwise only runs on CANFAR:
  * inventory enumeration -> per-freq_id fan-out -> stage -> reduce -> product;
  * resume (re-running is a no-op);
  * self-healing -- a freq_id whose fetches all fail is left incomplete and a
    nonzero exit is returned, then completed on a later run once the "outage"
    clears (resume retries what was never recorded as done);
  * quarantine -- an unreadable file is excluded + recorded, the run still
    completes, and a re-run skips it without re-fetching;
  * dedup -- duplicate inventory rows collapse to a single fetch.

Network sleeps are patched out so retries don't slow the test.

Run:  PYTHONPATH=src python tests/test_cadc_offline.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile

import numpy as np

from datatrawl.plugins.readers._baseband_format import FS, NFFT, make_synth_file
from datatrawl import instruments as inst_mod
import datatrawl.cli as cli
from datatrawl.plugins.sources import cadc_datatrail

F_TONE_BB = 12000.0          # injected baseband tone (Hz)
DF_HZ = FS / NFFT            # FFT bin width (~23.84 Hz)
FREQ_IDS = (614, 706)        # two explicit freq_ids for the archive scan


class FakeStorageClient:
    """Stand-in for cadcdata.StorageInventoryClient: cadcget over a fixture dir."""

    def __init__(self, fixture_dir: str, fail_always=()):
        self.fixture_dir = fixture_dir
        self.fail_always = set(fail_always)   # basenames that fail every call
        self.calls = 0

    def cadcget(self, uri, dest):
        self.calls += 1
        base = str(uri).rsplit("/", 1)[-1]
        if base in self.fail_always:
            raise RuntimeError(f"simulated CADC outage fetching {base}")
        src = os.path.join(self.fixture_dir, base)
        if not os.path.exists(src):
            raise FileNotFoundError(base)
        shutil.copy2(src, dest)


def build_fixture(d: str):
    """Synthetic baseband for two freq_ids + a matching inventory.jsonl.

    Inventory records use the real cadc-datatrail schema; the URIs the source
    builds from them resolve (by basename) to the fixture files the fake client
    serves. Each file carries a tone at F_TONE_BB so the PSD product is checkable.
    """
    fix = os.path.join(d, "fixture"); os.makedirs(fix)
    inst = inst_mod.load_instrument("chime")
    inv = os.path.join(d, "inventory.jsonl")
    files_by_freq_id: dict = {}
    with open(inv, "w") as fh:
        for m, ch in enumerate(FREQ_IDS):
            f_center = inst.freq_of_freq_id(ch) * 1e6
            files_by_freq_id.setdefault(ch, [])
            for k in range(2):
                event = f"astro_{m}{k}"
                name = f"baseband_{event}_{ch}.h5"
                path = os.path.join(fix, name)
                make_synth_file(path, 6 * NFFT, 32, f_center / 1e6,
                                F_TONE_BB, seed=10 * m + k + 1)
                files_by_freq_id[ch].append(name)
                fh.write(json.dumps(dict(
                    freq_id=ch, event=event, common_path="cadc:TEST/fixture",
                    size_bytes=os.path.getsize(path),
                    obs_date=f"2024-0{m + 1}-1{k}")) + "\n")
    return fix, inv, sorted(files_by_freq_id), files_by_freq_id


@contextlib.contextmanager
def fake_cadc(fake: FakeStorageClient):
    """Make the real source use `fake` and skip retry sleeps, then restore."""
    orig_mk = cadc_datatrail.CadcDatatrailSource._make_client
    orig_sleep = cadc_datatrail.time.sleep
    cadc_datatrail.CadcDatatrailSource._make_client = lambda self, cert=None: fake
    cadc_datatrail.time.sleep = lambda *a, **k: None
    try:
        yield
    finally:
        cadc_datatrail.CadcDatatrailSource._make_client = orig_mk
        cadc_datatrail.time.sleep = orig_sleep


def _scan(inv, root, sel, tmp) -> int:
    return cli.main([
        "scan", "--telescope", "chime", "--source", "cadc-datatrail",
        "--inventory", inv, "--reader", "chime-baseband",
        "--analyzer", "spectrum", "--select", sel, "--root", root,
        "--tmp-dir", tmp, "--checkpoint-every", "1",
    ])


def _product_path(root, ch) -> str:
    return os.path.join(root, "results", "chime", "spectrum", f"{ch}.npz")


def _products_ok(root, freq_ids, expect_files=2) -> bool:
    ok = True
    for ch in freq_ids:
        p = _product_path(root, ch)
        if not os.path.exists(p):
            print(f"  FAIL: missing product {ch}.npz"); ok = False; continue
        z = np.load(p, allow_pickle=False)
        if int(z["freq_id"]) != ch:
            print(f"  FAIL: {ch}.npz freq_id={int(z['freq_id'])}"); ok = False
        if int(z["files"].size) != expect_files:
            print(f"  FAIL: {ch}.npz files={int(z['files'].size)}, "
                  f"expected {expect_files}"); ok = False
        psd, freqs = z["psd"], z["freqs_hz"]
        peak = freqs[int(np.argmax(psd))]
        if abs(peak - F_TONE_BB) > 2 * DF_HZ:
            print(f"  FAIL: {ch}.npz PSD peak {peak:+.1f} vs tone {F_TONE_BB:+.0f}")
            ok = False
    return ok


def run_offline() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_cadc_")
    fix, inv, freq_ids, files_by_freq_id = build_fixture(work)
    sel = ",".join(str(c) for c in freq_ids)
    ok = True

    # 1. full scan through the REAL cadc-datatrail source, network faked --------
    root = os.path.join(work, "run1")
    with fake_cadc(FakeStorageClient(fix)):
        if _scan(inv, root, sel, os.path.join(work, "t1")) != 0:
            print("  FAIL: scan returned nonzero"); ok = False
    if not _products_ok(root, freq_ids):
        ok = False
    # resume = no-op
    with fake_cadc(FakeStorageClient(fix)):
        if _scan(inv, root, sel, os.path.join(work, "t1")) != 0:
            print("  FAIL: resume returned nonzero"); ok = False
    print(f"  archive scan: {len(freq_ids)} freq_ids via real cadc-datatrail "
          f"(faked cadcget), resume clean")

    # 2. self-healing across runs ----------------------------------------------
    # Make every fetch for the highest freq_id fail in run 1: that product is
    # left incomplete and the scan exits nonzero. Run 2 with the "outage" cleared
    # must complete it via resume (the failed files were never recorded done).
    root2 = os.path.join(work, "run2")
    bad_freq_id = freq_ids[-1]
    bad = files_by_freq_id[bad_freq_id]
    with fake_cadc(FakeStorageClient(fix, fail_always=bad)):
        if _scan(inv, root2, sel, os.path.join(work, "t2")) == 0:
            print("  FAIL: expected nonzero exit while a freq_id was failing")
            ok = False
    good_path = _product_path(root2, freq_ids[0])
    bad_path = _product_path(root2, bad_freq_id)
    if not os.path.exists(good_path):
        print("  FAIL: healthy freq_id should have completed in run 1"); ok = False
    if os.path.exists(bad_path) and int(np.load(bad_path)["files"].size) == 2:
        print("  FAIL: failing freq_id should NOT be complete after run 1"); ok = False
    with fake_cadc(FakeStorageClient(fix)):            # outage cleared
        if _scan(inv, root2, sel, os.path.join(work, "t2")) != 0:
            print("  FAIL: heal run returned nonzero"); ok = False
    if not _products_ok(root2, freq_ids):
        ok = False
    print(f"  self-healing: freq_id {bad_freq_id} failed in run 1, recovered on "
          f"re-run (resume retried the un-recorded files)")

    print("CADC OFFLINE SELF-TEST PASSED" if ok else "CADC OFFLINE SELF-TEST FAILED")
    return 0 if ok else 1


def test_cadc_datatrail_offline():
    """pytest entry point: the real archive path works offline and self-heals."""
    assert run_offline() == 0


# ---------------------------------------------------------------------------
# Quarantine: a file that won't read (bad header) is excluded + recorded, the
# run still completes, and a re-run skips it without re-fetching.
# ---------------------------------------------------------------------------
def _read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def _single_freq_id_fixture(d, good_events, freq_id=614, extra_rows=False):
    """One freq_id: synth files for good_events, return (fix, inv, ch, names)."""
    fix = os.path.join(d, "fixture"); os.makedirs(fix)
    inst = inst_mod.load_instrument("chime")
    inv = os.path.join(d, "inventory.jsonl")
    ch = freq_id
    f_center = inst.freq_of_freq_id(ch) * 1e6
    names = []
    with open(inv, "w") as fh:
        for i, event in enumerate(good_events):
            name = f"baseband_{event}_{ch}.h5"
            make_synth_file(os.path.join(fix, name), 6 * NFFT, 32,
                            f_center / 1e6, F_TONE_BB, seed=i + 1)
            names.append(name)
            rec = dict(freq_id=ch, event=event, common_path="cadc:TEST/fixture",
                       size_bytes=os.path.getsize(os.path.join(fix, name)),
                       obs_date=f"2024-01-0{i}")
            fh.write(json.dumps(rec) + "\n")
            if extra_rows:                       # duplicate inventory row
                fh.write(json.dumps(rec) + "\n")
    return fix, inv, ch, names


def run_quarantine() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_quar_")
    fix, inv, ch, good = _single_freq_id_fixture(work, ["good_0", "good_1"])
    ok = True

    # add one file with a bad header (not valid HDF5) + its inventory row
    bad = f"baseband_corrupt_0_{ch}.h5"
    with open(os.path.join(fix, bad), "wb") as bf:
        bf.write(b"NOT-an-HDF5-file: truncated/garbage header\n" * 64)
    with open(inv, "a") as fh:
        fh.write(json.dumps(dict(freq_id=ch, event="corrupt_0",
            common_path="cadc:TEST/fixture",
            size_bytes=os.path.getsize(os.path.join(fix, bad)),
            obs_date="2024-01-09")) + "\n")

    root = os.path.join(work, "run")
    qpath = os.path.join(
        root, "results", "chime", "quarantine",
        "cadc-datatrail--chime-baseband.jsonl",
    )

    fake1 = FakeStorageClient(fix)
    with fake_cadc(fake1):
        rc = _scan(inv, root, str(ch), os.path.join(work, "t"))
    if rc != 0:
        print(f"  FAIL: a bad header must NOT fail the run (rc={rc})"); ok = False
    prod = _product_path(root, ch)
    if not os.path.exists(prod):
        print("  FAIL: product missing"); ok = False
    elif int(np.load(prod)["files"].size) != 2:
        print(f"  FAIL: product has {int(np.load(prod)['files'].size)} files, "
              "expected the 2 good ones"); ok = False
    recs = _read_jsonl(qpath)
    if len(recs) != 1 or recs[0].get("name") != bad:
        print(f"  FAIL: expected 1 quarantine record for {bad}, got {recs}")
        ok = False
    elif "probe/read" not in recs[0].get("reason", ""):
        print(f"  FAIL: unexpected quarantine reason {recs[0].get('reason')!r}")
        ok = False

    # re-run: the bad file is already quarantined -> skipped with no re-fetch,
    # the run is clean, and the ledger is not duplicated
    fake2 = FakeStorageClient(fix)
    with fake_cadc(fake2):
        rc2 = _scan(inv, root, str(ch), os.path.join(work, "t"))
    if rc2 != 0:
        print(f"  FAIL: re-run should be clean (rc={rc2})"); ok = False
    if fake2.calls != 0:
        print(f"  FAIL: re-run re-fetched {fake2.calls} file(s); done+quarantined "
              "should skip everything"); ok = False
    if len(_read_jsonl(qpath)) != 1:
        print("  FAIL: quarantine ledger duplicated on re-run"); ok = False

    print(f"  quarantine: bad-header {bad} excluded + recorded, run clean, "
          "re-run skipped it without re-fetching")
    print("QUARANTINE SELF-TEST PASSED" if ok else "QUARANTINE SELF-TEST FAILED")
    return 0 if ok else 1


def run_dedup() -> int:
    work = tempfile.mkdtemp(prefix="datatrawl_dedup_")
    # two unique files, but every inventory row written twice
    fix, inv, ch, names = _single_freq_id_fixture(
        work, ["e_0", "e_1"], extra_rows=True)
    ok = True
    raw = [l for l in open(inv) if l.strip()]
    if len(raw) != 4:
        print(f"  FAIL: setup expected 4 inventory rows, got {len(raw)}"); ok = False

    root = os.path.join(work, "run")
    fake = FakeStorageClient(fix)
    with fake_cadc(fake):
        rc = _scan(inv, root, str(ch), os.path.join(work, "t"))
    if rc != 0:
        print(f"  FAIL: rc {rc}"); ok = False
    prod = _product_path(root, ch)
    if int(np.load(prod)["files"].size) != 2:
        print(f"  FAIL: dedup -> expected 2 files, product has "
              f"{int(np.load(prod)['files'].size)}"); ok = False
    if fake.calls != 2:
        print(f"  FAIL: dedup -> expected 2 fetches, client saw {fake.calls}")
        ok = False

    print("  dedup: 4 inventory rows -> 2 unique files fetched + processed once")
    print("DEDUP SELF-TEST PASSED" if ok else "DEDUP SELF-TEST FAILED")
    return 0 if ok else 1


def test_quarantine_bad_header():
    """pytest entry point: an unreadable file is quarantined, not fatal."""
    assert run_quarantine() == 0


def test_inventory_dedup():
    """pytest entry point: duplicate inventory rows collapse to one fetch."""
    assert run_dedup() == 0


if __name__ == "__main__":
    rc = run_offline()
    rc = run_quarantine() or rc
    rc = run_dedup() or rc
    sys.exit(rc)
