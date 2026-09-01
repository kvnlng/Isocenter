"""Every exported instance warned about a membership test that could never pass (#248).

`_export_instance_worker` asked `if "_ISOCENTER_REDACTION_HASH" in ds:`
on a pydicom `Dataset`. `Dataset.__contains__` rejects a string that is
neither a tag nor a DICOM keyword and emits::

    UserWarning: Invalid value '_ISOCENTER_REDACTION_HASH' used with the
    'in' operator: must be an element tag as a 2-tuple or int, or an
    element keyword

once per exported instance -- 10k instances, 10k identical warnings --
into the host application's warning stream, because this package
deliberately installs no global warning filter (#144). The guard was also
dead: `_merge` skips every `_`-prefixed key, and a string that cannot be
parsed as a tag or keyword cannot *be* a `Dataset` member, so the `del`
under it never ran. Both tests here measure that on the real worker with
a redaction-attested instance, rather than trusting the reading (#248
asked for the measurement explicitly).

Two traps this file has to dodge, stated so nobody "simplifies" them away:

- **pytest.ini carries `filterwarnings = ignore:::pydicom.*`**, which is
  why ~600 tests ran the dead line for years without seeing the warning.
  Each test overrides the filter for itself with `warnings.catch_warnings`;
  lose that and the regression test goes green against the bug.
- **`session.export()` always runs the worker in spawned subprocesses**
  (`export_batch` passes `maxtasksperchild=25`, and worker recycling rules
  threads out in `_use_threads` however the env is set), so a warning
  raised in the worker is invisible to this process's `warnings` machinery.
  The tests therefore call `_export_instance_worker` in-process, on
  contexts built by `_generate_export_contexts` -- the same planner both
  public export paths feed the same worker from.
"""
import warnings

from isocenter.io_handlers import DicomExporter, _export_instance_worker

#: Inside the fixture's 32x32 image, so the zone lands, `modified` goes
#: True, and the redaction attestation -- the `_ISOCENTER_REDACTION_HASH`
#: attribute this file is about -- is actually written.
IN_IMAGE_ZONE = [0, 8, 0, 8]

HASH_KEY = "_ISOCENTER_REDACTION_HASH"

#: The invariant fragment of pydicom's message. Matched instead of the
#: key name so a reintroduction under any spelling still trips the test.
IN_OPERATOR_WARNING = "used with the 'in' operator"


def _redacted_export_context(reloaded_redaction_session, tmp_path, name):
    """One ExportContext for an instance that genuinely carries the hash.

    Redaction first, so the export faces the very key the dead guard
    named -- an un-redacted instance would measure nothing.
    """
    session, inst = reloaded_redaction_session([IN_IMAGE_ZONE], name=name)
    applied = session.redact(show_progress=False)
    assert applied == 1, "fixture drift: the zone no longer lands"
    assert inst.attributes.get(HASH_KEY), (
        "fixture drift: redaction no longer writes the attestation this "
        "file exists to measure against")

    patient = session.store.patients[0]
    contexts = DicomExporter._generate_export_contexts(
        patient, patient.studies, str(tmp_path / f"{name}_out"))
    assert len(contexts) == 1
    return contexts[0]


def test_exporting_a_redacted_instance_emits_no_in_operator_warning(
        reloaded_redaction_session, tmp_path):
    """The host application's warning stream stays clean (#248)."""
    ctx = _redacted_export_context(
        reloaded_redaction_session, tmp_path, "warn_stream")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        outcome = _export_instance_worker(ctx)

    assert outcome.ok, f"export failed: {outcome.error}"
    noise = [str(w.message) for w in caught
             if issubclass(w.category, UserWarning)
             and IN_OPERATOR_WARNING in str(w.message)]
    assert not noise, (
        "exporting one instance put pydicom membership-test noise on the "
        f"caller's warning stream: {noise}")


def test_the_hash_key_is_never_a_dataset_member(
        reloaded_redaction_session, tmp_path, monkeypatch):
    """The measurement #248 asked for: the deleted guard could never fire.

    Captures the dataset at `_finalize_dataset` -- the first call after
    the site the dead `if`/`del` occupied -- on a run where the instance
    verifiably carries `_ISOCENTER_REDACTION_HASH`, and measures the exact
    condition the deleted line tested. It is False: `_merge` dropped the
    `_`-prefixed key before the dataset ever saw it, so deleting the guard
    and its `del` changed no exported byte. This test is what holds that
    still if `_merge` ever stops skipping underscore keys.
    """
    ctx = _redacted_export_context(
        reloaded_redaction_session, tmp_path, "membership")

    seen = {}
    real_finalize = DicomExporter._finalize_dataset

    def capturing_finalize(ds, compression=None, pixel_array=None):
        with warnings.catch_warnings():
            # The measurement itself performs the invalid membership test;
            # keep its own noise out of the run being measured.
            warnings.simplefilter("ignore")
            seen["member"] = HASH_KEY in ds
        return real_finalize(ds, compression, pixel_array=pixel_array)

    monkeypatch.setattr(
        DicomExporter, "_finalize_dataset", staticmethod(capturing_finalize))

    outcome = _export_instance_worker(ctx)

    assert outcome.ok, f"export failed: {outcome.error}"
    assert seen["member"] is False, (
        "the redaction attestation reached the pydicom Dataset; the "
        "underscore-key skip in _merge no longer holds and the exported "
        "file may carry a private bookkeeping element")
