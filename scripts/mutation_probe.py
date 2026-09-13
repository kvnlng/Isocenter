"""Sampled mutation probe: does the suite notice when behaviour changes?

Coverage says a line ran. It does not say a test would have failed had
that line done something else. This asks the second question: mutate one
decision in a module, run only the tests that import it, and see whether
anything goes red. A mutant that survives is a change to production
behaviour the suite cannot see.

Run it against the de-identification core, where a test that cannot fail
is not an inconvenience but a leak nobody is watching for:

    python -m scripts.mutation_probe                 # default targets, each at its own budget
    python -m scripts.mutation_probe 10              # one budget for every module: the cheap pass
    python -m scripts.mutation_probe 30 isocenter/session.py tests/test_session.py

A default run is long, and survivors are what make it so: a surviving
mutant pays its module's whole test list, where a kill exits at the
first red test. Measured on 3.12.14, as upper bounds -- every sampled
mutant surviving:

    default run, before the session/entities/imagecodecs rows   ~3.7 h
    default run, with them (#414, #419)                         ~8.4 h
    the eleven #439 rows, on top of that                       +~1.1 h
    default run, with the #439 rows too                         ~9.5 h
    `python -m scripts.mutation_probe 10`, the cheap pass       ~3.0 h

`session.py` and `entities.py` each list over 120 test files, one full
pass of ~300s and ~190s. A mutant that hangs costs up to three times its
module's control pass, 30s minimum (`mutant_timeout()`), and is reported
as TIMEOUT and counted as detected (#442). A real run
is much shorter, since most mutants die in seconds, but the survival
rates any such estimate rests on were sampled too thinly to print here.
The #439 rows' figure was measured with other suites running, so it is
upper-side.
The positional budget is the override for every module at once; nothing
in CI runs this script.

A default run ends by printing `NOT_PROBED`: the modules it does not
measure, and why. `tests/test_mutation_probe_targets.py` fails when a
module under `isocenter/` is in neither `TARGETS` nor `NOT_PROBED`.

**Sampled, not exhaustive.** It walks mutation sites at a fixed stride,
so the output is "of N representative mutations, M survived" -- evidence
about whether the suite bites, not a mutation score to track over time.
Adding an operator renumbers the sites, so two runs across such a change
are not comparable sample-for-sample.

**The operator set decides what can be measured.** Three operators see
decisions -- comparison flips, `and`/`or`, boolean constants -- and three
see straight-line code: dropping a `not`, replacing a returned value with
None, and deleting a bare expression statement. That second group exists
because the first reported `crypto.py` as 0 sites and 0 survivors: 73
lines of key derivation, encrypt and decrypt with almost no branching,
so there was nothing for a comparison flip to find. A module with 0
sites is unmeasured, not clean, and the run says so rather than printing
a zero and letting it be read as a pass (#106).

Still unreached: argument swaps between same-typed parameters, string and
bytes constant mutation (salts, encodings, key-derivation parameters),
and exception-handler removal.

**A survivor is a question, not a verdict.** Some mutants are equivalent
-- they change the code without changing behaviour, and no test could
tell. Read each one before acting on it. The ones that matter are where
the mutated code is still plausible *and* the behaviour differs.

**The control run is load-bearing.** Before mutating, the module is
unparsed from its own AST and the tests are run against that. If the
control fails, the harness itself is perturbing the module and every
result below it is an artefact -- so it stops rather than reporting.

The mutated file is written in place and restored in a `finally`. Run it
on a clean tree so `git checkout isocenter/` is always a way out.

**A verdict is only about the mutation if the mutation was compiled.**
Runs carry `PYTHONDONTWRITEBYTECODE=1` and every write is checked against
CPython's own `.pyc` validation rule before the tests see it, because a
stale bytecode cache made this tool report mutations that were never in
the code it tested. `assert_fresh` has the mechanism (#174). The cache it
inspects is the one `PYTEST[0]` itself names, because the entry that
matters belongs to the interpreter that runs the tests, not the one that
launched the probe (#201).
"""

import ast, importlib.util, os, signal, struct, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTEST = [str(REPO / ".venv/bin/python"), "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:randomly"]

# The `(tests, budget)` to use for each target. Complete-ness of the
# test list matters more than it looks: a mutant that a test in this
# repo would kill, but which is not in that list, is reported as
# SURVIVED -- a phantom gap in the de-identification core that costs a
# human a real investigation.
# `tests/test_mutation_probe_targets.py` fails if a test file imports a
# target module and is not listed here. Extra entries are allowed but
# must have earned their runtime with a measured kill: every listed file
# runs for every sampled mutant. `test_remediation_actions.py` exercises
# `remediation.py` without importing it, which no import scan can see;
# `test_redaction_export.py` and `test_reversibility.py` do the same for
# `io_handlers.py` (the `apply_redaction_to_array` call and the
# `(0400,0510)` write, both verified kills).
#
# That is the whole rule for reach the scan cannot see (#441): the scan
# stays the only thing the guard demands, and a file beyond it joins a
# row only with a measured kill, named in the row's comment. Both halves
# were measured. Without such files the probe reports survivors that are
# not there: on the two files the scan names, the reversibility.py row
# prints 6 survivors of 18, and a file that reaches the module through
# the session kills two of them. And a runtime trace
# is no replacement demand, because executing a line is not being able to
# kill a mutant of it: it would put 127 test files on store.py's row and
# 168 on logger.py's.
#
# Where candidates come from: a per-test coverage trace. Run the suite
# under `.coveragerc`'s [run] section plus `dynamic_context =
# test_function`, `coverage combine`, and for the module take the test
# files named by the contexts in `CoverageData.contexts_by_lineno(path)`,
# minus what `_importers` already demands. That is a candidate list, not
# a row: run the module's sample against the candidates, and keep only a
# file some mutant dies on. Spawned workers carry no dynamic context, so a
# test that reaches the module only inside a `run_parallel()` worker is
# missing from the trace. One trace of the whole suite took 68 minutes
# (0e3e38c, 3.12.14, on a loaded machine).
# The budget is per module because the knob does two jobs at once
# globally: `io_handlers.py` has ~5x the sites of any other target, so
# raising its sampling density with one shared number forces the
# already-measured modules to re-pay at stride 1. The positional CLI
# budget overrides every module for one run.
TARGETS = {
    # 68 sites; budget 80 is stride 1 with headroom (#365). Five files
    # rather than the issue's three: test_every_test_that_imports_a_target_module_is_listed
    # demands every file whose text names `isocenter.parallel`, and
    # test_logging.py and test_shared_executor_lifecycle.py do.
    #
    # The last two are listed although the guard does not demand them
    # (#384, #400): they cover the threads-or-processes decision through
    # `redact()`, which is where its two attribution fields are read, and
    # a probe that did not run them would report a survivor for exactly
    # the mutation those files exist to kill -- an attribution computed
    # after the `force_threads` short-circuit. Extras cost the guard
    # nothing.
    "isocenter/parallel.py": (["tests/test_logging.py",
                               "tests/test_packaging_contract.py",
                               "tests/test_parallel_config.py",
                               "tests/test_parallel_contract.py",
                               "tests/test_redaction_worker_count.py",
                               "tests/test_shared_executor_lifecycle.py",
                               "tests/test_redaction_names_its_strategy.py",
                               "tests/test_memory_store_reports_its_processes_lever.py",
                               "tests/test_duplicate_sop_uid_at_ingest.py"],
                              80),
    "isocenter/crypto.py": (["tests/test_crypto.py", "tests/test_reversibility.py"], 30),
    # --- The #439 rows. Each was measured at budget 30 on 3.12.14 with other
    # suites running on the same machine, so the seconds are upper-side.
    # Every list holds what `_importers` demands; a file beyond it is named
    # with the mutant it kills (#441, the rule in the comment above).
    #
    # 17 sites, exhaustive: all 17 killed. One file, 1.4s per pass.
    "isocenter/automation.py": (["tests/test_automation.py"], 30),
    # 5 sites, exhaustive: all 5 killed. Seven files, 20.3s per pass.
    "isocenter/exporters/__init__.py": (["tests/test_exporter_registry.py",
                                         "tests/test_murmur_annotations.py",
                                         "tests/test_study_date_roundtrip.py",
                                         "tests/test_wfdb_option_strictness.py",
                                         "tests/test_wfdb_partial_export_is_audited.py",
                                         "tests/test_wfdb_start_date_honesty.py",
                                         "tests/test_wfdb_writer.py"], 30),
    # 11 sites, exhaustive: all 11 killed. Two files, 46.0s per pass.
    "isocenter/blob_kind.py": (["tests/test_blob_kind_grammar.py",
                                "tests/test_nested_pixel_carriage.py"], 30),
    # 16 sites, exhaustive: all 16 killed. Twelve files, 18.5s per pass.
    "isocenter/builders.py": (["tests/test_entities.py", "tests/test_export_contract.py",
                               "tests/test_full_logging.py", "tests/test_io.py",
                               "tests/test_phi_retention.py", "tests/test_phi_status.py",
                               "tests/test_recursive_import.py",
                               "tests/test_report_action_evidence.py",
                               "tests/test_safe_export_feedback.py",
                               "tests/test_save_all_contract.py",
                               "tests/test_scaffolding.py",
                               "tests/test_tag_key_normalisation.py"], 30),
    # 99 sites; budget 30 is a stride of 3, 33 mutants: all 33
    # killed. Seven files, 75.6s per pass -- the slowest of the #439 rows,
    # measured with other suites on the machine.
    "isocenter/pixel_geometry.py": (["tests/test_automation.py",
                                    "tests/test_descriptor_edit_with_pixels_unloaded.py",
                                    "tests/test_float_pixel_data_export.py",
                                    "tests/test_pixel_dtype_roundtrip.py",
                                    "tests/test_pixel_geometry.py",
                                    "tests/test_redaction_robustness.py",
                                    "tests/test_verification_logic.py"], 30),
    # 2 sites, exhaustive: both killed. No test names this module -- it is
    # reached through `export(format="dicom")` -- so the scan demands
    # nothing and both files are hand extras (#441). Each was measured to
    # kill both mutants alone, so either would do; two, because a row
    # resting on one file is one refactor of that file from empty. 10.3s
    # per pass for the pair.
    "isocenter/exporters/dicom.py": (["tests/test_api_coherence.py",
                                      "tests/test_export_contract.py"], 30),
    # 7 sites, exhaustive: all 7 killed, measured in the review of #491.
    # The survivor #439 filed as #486 -- `ManifestItem.anonymized: bool =
    # True` flipped to False -- was the defect that issue fixed: the
    # default is False now, and the mutant flipping it back to True dies
    # on test_manifest_says_what_was_done.py::
    # test_a_manifest_item_nobody_described_is_not_anonymized. Two files.
    "isocenter/manifest.py": (["tests/test_manifest.py",
                               "tests/test_manifest_says_what_was_done.py"], 30),
    # 24 sites, exhaustive: 23 killed. Three files, 6.6s per pass.
    #   - `_coverage`'s `x_right <= x_left or y_bottom <= y_top` weakened
    #     to `and` survived until #439, and is not equivalent: a box clear
    #     of the zone on one axis only falls through to the area product
    #     and gets a negative coverage (-9.0 in the test), outside the
    #     0.0-1.0 range `is_covered` documents, so `is_covered(...,
    #     threshold=0.0)` turns false for it. `_findings_for` is immune
    #     (its best coverage starts at 0.0 and only rises). Killed since by
    #     tests/test_verification_logic.py::
    #     test_text_clear_of_a_zone_has_zero_coverage_never_a_negative_one.
    #   - The survivor, `_coverage`'s `if text_area <= 0: return 0.0` with
    #     the value replaced by None, is equivalent because it is
    #     unreachable. `tw <= 0` makes `x_right = min(tx + tw, zx2) <= tx
    #     <= x_left`, so the guard above has already returned, and `th <= 0`
    #     is the same on the other axis; the second guard only ever sees a
    #     positive area. Nothing is filed: the guard costs nothing.
    "isocenter/verification.py": (["tests/test_ocr_formal.py",
                                   "tests/test_scan_reports_what_it_could_not_read.py",
                                   "tests/test_verification_logic.py"], 30),
    # 13 sites, exhaustive: 12 killed (10 before #439's Type 2 test). Five
    # files, 5.7s per pass.
    #   - The Type 2 check inverted (`req == '2'` to `!=`) and its
    #     `errors.append` deleted both survived until #439: nothing built a
    #     CT missing a Type 2 element. Killed since by
    #     tests/test_validation.py::
    #     test_a_missing_type_2_element_is_reported_and_an_empty_one_is_not.
    #   - The survivor, `if sop not in IODValidator._SOP_RULES: return []`
    #     returning None, is equivalent: the one caller,
    #     `DicomExporter._finalize_dataset` in io_handlers.py, only
    #     truth-tests the result.
    "isocenter/validation.py": (["tests/test_export_error.py",
                                 "tests/test_floor_policy.py", "tests/test_io.py",
                                 "tests/test_structured_export.py",
                                 "tests/test_validation.py", "tests/test_wfdb_writer.py",
                                 "tests/test_the_project_secret_lives_in_the_store.py"], 30),
    # Promoted out of NOT_PROBED by #495/#456, whose tests exercise both
    # modules' behaviour rather than their shapes: the loader's refusals
    # (tests/test_load_config_raises.py) and the floor seed, the lowercase
    # key and the `none` round trip (tests/test_floor_policy.py). The
    # lists are exactly the importers the scan demands -- no hand extras.
    # Their NOT_PROBED notes (config_manager 29/40 killed, configuration
    # 21/31) were measured before either bunch and are not carried over:
    # the first default run is the new measurement.
    "isocenter/config_manager.py": (["tests/test_api_coherence.py",
                                     "tests/test_config.py",
                                     "tests/test_custom_profiles.py",
                                     "tests/test_documented_zones_are_zone_space.py",
                                     "tests/test_floor_policy.py",
                                     "tests/test_load_config_raises.py",
                                     "tests/test_profiles.py",
                                     "tests/test_scaffold_features.py",
                                     "tests/test_shipped_resource_is_required.py",
                                     "tests/test_structured_export.py",
                                     "tests/test_suggested_config.py",
                                     "tests/test_zone_validation.py"], 30),
    "isocenter/configuration.py": (["tests/test_automation.py",
                                    "tests/test_configuration_manual.py",
                                    "tests/test_configuration_persistence.py",
                                    "tests/test_documented_zones_are_zone_space.py",
                                    "tests/test_floor_policy.py",
                                    "tests/test_redaction_export.py"], 30),
    # 18 sites, exhaustive: 15 killed. Three files, 2s per pass.
    #
    # tests/test_relock_identity_token.py is a hand extra (#441): it reaches
    # the module through `Session.lock_identities`, which the scan cannot
    # see, and kills two mutants nothing else does -- the deleted
    # `item.set_attr(self.TAG_TRANSFER_SYNTAX_UID, ...)` (its
    # test_the_token_item_names_its_payload_transfer_syntax) and the
    # deleted `instance.mark_modified()` (its
    # test_a_re_lock_reaches_the_store). On the two demanded files alone
    # the row prints 6 survivors. tests/test_reversibility.py was measured
    # as a candidate too, and stays off: before #439's tests it killed
    # six mutants, and after them it kills nothing these three miss.
    #
    # Three more were gaps until #439 and are killed since by
    # tests/test_relock_identity_token.py and
    # tests/test_reversibility_coverage.py: the transfer-syntax write
    # above, the WARNING that says an item has no Encrypted Content, and
    # the ERROR that names the instance a wrong key failed.
    #
    # The deleted ERROR in `embed_identity_token` was a fourth survivor,
    # classed equivalent because a `raise` follows carrying the same
    # exception; #487 pinned the line's text (it is what a reader of the
    # log gets, and it named a bare raise with an empty tail), and
    # tests/test_reversibility_coverage.py kills it since.
    #
    # The three survivors, all inside `embed_original_data`: its call to
    #     `self.embed_identity_token(instance, token)`, its DEBUG line, and
    #     the ERROR in its `except`, which re-raises. The method has no
    #     caller in isocenter/ and its two tests pass empty or raising
    #     input, so the whole wrapper is dead (#488).
    "isocenter/reversibility.py": (["tests/test_feature_regression.py",
                                    "tests/test_reversibility_coverage.py",
                                    "tests/test_relock_identity_token.py"], 30),
    # 7 sites, exhaustive: 6 killed. Five files, 43.3s per pass.
    #
    # Two are hand extras (#441), reaching the renderer through
    # `generate_report()`, which the scan cannot see. On the three
    # demanded files alone the row prints 3 survivors of 7, and the two
    # beyond the default are exactly these files' kills:
    #   - tests/test_export_delivery_counters.py kills
    #     `if report.instances_written is not None:` flipped to `is`
    #     (test_an_empty_plan_does_not_reuse_the_previous_exports_numbers);
    #   - tests/test_report_export_boundary.py kills the dropped `not` in
    #     `if not report.export_recorded:` (its
    #     test_a_report_generated_before_any_export_carries_the_boundary_note).
    #
    # The survivor, the dataclass default `export_recorded: bool = False`
    # flipped to True, is equivalent: the one construction, in
    # `Session.generate_report`, passes `export_recorded=` explicitly.
    "isocenter/reporting.py": (["tests/test_data_loss_reporting.py",
                                "tests/test_private_sequence_implicit_vr.py",
                                "tests/test_reporting.py",
                                "tests/test_report_export_boundary.py",
                                "tests/test_export_delivery_counters.py"], 30),
    "isocenter/privacy.py": (["tests/test_analysis.py", "tests/test_analysis_persistence.py",
                              "tests/test_a_pre_096_store_is_not_reshifted.py",
                              "tests/test_a_replaced_study_date_is_raised.py",
                              "tests/test_an_unshifted_date_is_raised.py",
                              "tests/test_audit_suppression.py", "tests/test_automation.py",
                              "tests/test_config_tags_shapes.py",
                              "tests/test_declined_date_recurs.py",
                              "tests/test_declined_remediation_is_recorded.py",
                              "tests/test_floor_policy.py",
                              "tests/test_multiprocessing.py",
                              "tests/test_mutation_gaps.py", "tests/test_ocr_formal.py",
                              "tests/test_nested_date_shifts_once.py",
                              "tests/test_one_value_per_owned_tag.py",
                              "tests/test_patient_level_remediation_reaches_instances.py",
                              "tests/test_persistence.py", "tests/test_privacy.py",
                              "tests/test_private_sequence_implicit_vr.py",
                              "tests/test_profile_end_to_end.py", "tests/test_remediation.py",
                              "tests/test_remediation_actions.py",
                              "tests/test_remediation_invariants.py",
                              "tests/test_scaffold_features.py",
                              "tests/test_shipped_resource_is_required.py",
                              "tests/test_sr_anonymization.py",
                              "tests/test_the_jitter_seed_survives_anonymize.py",
                              "tests/test_a_pre_097_store_keeps_one_offset_per_patient.py",
                              "tests/test_the_project_secret_keys_the_pseudonym_and_offset.py",
                              "tests/test_the_project_secret_lives_in_the_store.py",
                              "tests/test_nested_remediation_reaches_the_instance.py"], 30),
    "isocenter/remediation.py": (["tests/test_audit_suppression.py",
                                  "tests/test_a_replaced_study_date_is_raised.py",
                                  "tests/test_an_unshifted_date_is_raised.py",
                                  "tests/test_declined_date_recurs.py",
                                  "tests/test_declined_remediation_is_recorded.py",
                                  "tests/test_deid_tags.py",
                                  "tests/test_mutation_gaps.py",
                                  "tests/test_nested_date_shifts_once.py",
                                  "tests/test_patient_level_remediation_reaches_instances.py",
                                  "tests/test_persistence.py",
                                  "tests/test_private_sequence_implicit_vr.py",
                                  "tests/test_remediation.py",
                                  "tests/test_remediation_accounting.py",
                                  "tests/test_remediation_actions.py",
                                  "tests/test_remediation_dates.py",
                                  "tests/test_remediation_invariants.py",
                                  "tests/test_phi_retention.py",
                                  "tests/test_scaffold_features.py",
                                  # A hand extra (#441): it reaches
                                  # `apply_remediation` through
                                  # `Session.anonymize()` without importing
                                  # this module, and its
                                  # test_a_declined_finding_on_the_same_instance_says_false
                                  # is the one test that kills the pass-end
                                  # demotion deleted (measured, review of
                                  # #491); nothing the scan demands does.
                                  "tests/test_manifest_says_what_was_done.py",
                                  "tests/test_one_value_per_owned_tag.py",
                                  "tests/test_the_jitter_seed_survives_anonymize.py",
                                  "tests/test_a_pre_097_store_keeps_one_offset_per_patient.py",
                                  "tests/test_the_project_secret_keys_the_pseudonym_and_offset.py",
                                  "tests/test_the_project_secret_lives_in_the_store.py",
                                  "tests/test_nested_remediation_reaches_the_instance.py"], 30),
    "isocenter/io_handlers.py": (["tests/test_api_coherence.py",
                                  "tests/test_a_replaced_study_date_is_raised.py",
                                  "tests/test_audit_read_barrier.py",
                                  "tests/test_binary_retention_threshold.py",
                                  "tests/test_check_reversibility.py",
                                  "tests/test_codecs_strict.py",
                                  "tests/test_colour_space_at_ingest.py",
                                  "tests/test_compact_refuses_during_a_pass.py",
                                  "tests/test_compaction_races_a_concurrent_write.py",
                                  "tests/test_compaction_reclaims_a_row_instances_does_not_carry.py",
                                  "tests/test_compress_handlers.py",
                                  "tests/test_compress_j2k_coverage.py",
                                  "tests/test_data_loss_reporting.py",
                                  "tests/test_descriptor_edit_with_pixels_unloaded.py",
                                  "tests/test_dtype_only_replacement_survives_the_dedup.py",
                                  "tests/test_duplicate_sop_uid_at_ingest.py",
                                  "tests/test_empty_sequence_roundtrip.py",
                                  "tests/test_export_atomic_write.py",
                                  "tests/test_export_bits_stored.py",
                                  "tests/test_export_contract.py",
                                  "tests/test_export_date_error.py",
                                  "tests/test_export_delivery_counters.py",
                                  "tests/test_export_error.py",
                                  "tests/test_export_failure_audit.py",
                                  "tests/test_export_flushes_before_it_sweeps.py",
                                  "tests/test_export_loss_audit.py",
                                  "tests/test_export_merge_shape.py",
                                  "tests/test_export_photometric_admissibility.py",
                                  "tests/test_export_pixel_representation.py",
                                  "tests/test_export_pixels.py",
                                  "tests/test_export_readback.py",
                                  "tests/test_export_redaction_hash_warning.py",
                                  "tests/test_export_worker_graph_purity.py",
                                  "tests/test_export_ybr_full_422.py",
                                  "tests/test_float_pixel_data_export.py",
                                  "tests/test_ingest_failure_audit.py",
                                  "tests/test_ingest_imagecodecs_fallback.py",
                                  "tests/test_io.py",
                                  "tests/test_j2k_mct_label_on_export.py",
                                  "tests/test_legacy_waveform_hydration.py",
                                  "tests/test_logging.py",
                                  "tests/test_metadata_refactor_full.py",
                                  "tests/test_missing_study_date.py",
                                  "tests/test_multiprocessing.py",
                                  "tests/test_murmur_annotations.py",
                                  "tests/test_naming_structure.py",
                                  "tests/test_nested_phi_audit.py",
                                  "tests/test_nested_pixel_carriage.py",
                                  "tests/test_offset_table_frame_count.py",
                                  "tests/test_one_value_per_owned_tag.py",
                                  "tests/test_patient_level_remediation_reaches_instances.py",
                                  "tests/test_pixel_dtype_roundtrip.py",
                                  "tests/test_pixel_geometry_pipeline.py",
                                  "tests/test_planar_configuration_roundtrip.py",
                                  "tests/test_private_binary_ingest.py",
                                  "tests/test_private_tag_arity_roundtrip.py",
                                  "tests/test_private_tag_empty_value_roundtrip.py",
                                  "tests/test_private_tag_export.py",
                                  "tests/test_private_tag_vr_roundtrip.py",
                                  "tests/test_pydicom_deprecations.py",
                                  "tests/test_readback_label_admissibility.py",
                                  "tests/test_recursive_import.py",
                                  "tests/test_redaction_export.py",
                                  "tests/test_redaction_optimization.py",
                                  "tests/test_redaction_rgb.py",
                                  "tests/test_redaction_robustness.py",
                                  "tests/test_redaction_wildcard.py",
                                  "tests/test_remediation_accounting.py",
                                  "tests/test_reporting_features.py",
                                  "tests/test_reversibility.py",
                                  "tests/test_safe_export.py",
                                  "tests/test_services.py",
                                  "tests/test_session.py",
                                  "tests/test_shared_executor_lifecycle.py",
                                  "tests/test_signed_lossless_jpeg_decode.py",
                                  "tests/test_signed_pixels_survive_a_compressed_export.py",
                                  "tests/test_single_frame_encapsulated_decode.py",
                                  "tests/test_sidecar_gate_crosses_processes.py",
                                  "tests/test_sidecar_gate_order.py",
                                  "tests/test_sr_anonymization.py",
                                  "tests/test_structured_export.py",
                                  "tests/test_study_date_roundtrip.py",
                                  "tests/test_waveform_dicom_roundtrip.py",
                                  "tests/test_waveform_ingest.py",
                                  "tests/test_waveform_model.py",
                                  "tests/test_wfdb_conformance.py",
                                  "tests/test_wfdb_writer.py",
                                  "tests/test_worker_loss_is_reported.py",
                                  "tests/test_ybr_jpegls_read_doors.py",
                                  "tests/test_ybr_read_door_labels.py",
                                  "tests/test_the_project_secret_lives_in_the_store.py"], 30),
    # 453 sites. Until #383 this module had no row at all, so no mutant
    # of `_hold_sidecar_gate`, `_hold_pass_lock`, `_refuse_while_pass_open`,
    # `_flock_within`, `_SIDECAR_GATE_TIMEOUT_S`, the `:memory:` temp-file
    # ownership flag or `compact_sidecar()` was ever generated -- the
    # sidecar gate and the pass-lock #368 put here were invisible to the
    # probe.
    #
    # Budget 30 matches io_handlers.py deliberately: 453 sites at 30 is a
    # stride of 15, and io_handlers.py's 519 at 30 is a stride of 17, so
    # the two largest modules are sampled at comparable density. A
    # different number here would need a reason.
    #
    # This row roughly doubles a default probe run: 14 of the 42 files
    # alone take 19s (77 tests, 3.12.14), and all 42 run well over a
    # minute, so this target is ~30-45 minutes at budget 30. That is the
    # same order as io_handlers.py, and the probe is a by-hand tool
    # rather than CI, so it is affordable -- written down because an
    # unexplained doubling of the run time is the kind of thing someone
    # later "fixes" by cutting the budget.
    #
    # The list is measured, not curated: every file whose text matches
    # `isocenter\.persistence\b` (the `\b` correctly excludes
    # persistence_manager). #383 named eight; all eight are here and so
    # are 34 others, each of which the guard demands.
    "isocenter/persistence.py": (["tests/test_api_coherence.py",
                                  "tests/test_a_pre_096_store_is_not_reshifted.py",
                                  "tests/test_a_replaced_study_date_is_raised.py",
                                  "tests/test_an_unshifted_date_is_raised.py",
                                  "tests/test_async_persistence.py",
                                  "tests/test_audit_drop_accounting.py",
                                  "tests/test_audit_read_barrier.py",
                                  "tests/test_audit_worker_does_not_pin_its_store.py",
                                  "tests/test_blob_storage.py",
                                  "tests/test_bytes_persistence.py",
                                  "tests/test_close_does_not_drop_an_orphaned_save.py",
                                  "tests/test_compact_refuses_during_a_pass.py",
                                  "tests/test_compaction_races_a_concurrent_write.py",
                                  "tests/test_compaction_reclaims_a_row_instances_does_not_carry.py",
                                  "tests/test_concurrency_stress.py",
                                  "tests/test_dataframe_export.py",
                                  "tests/test_declined_remediation_is_recorded.py",
                                  "tests/test_dtype_only_replacement_survives_the_dedup.py",
                                  "tests/test_export_flushes_before_it_sweeps.py",
                                  "tests/test_float_pixel_data_export.py",
                                  "tests/test_flush_orphan_recovery.py",
                                  "tests/test_json_serialization.py",
                                  "tests/test_legacy_waveform_hydration.py",
                                  "tests/test_memory_store_unlinks_its_temp_files.py",
                                  "tests/test_nested_date_shifts_once.py",
                                  "tests/test_packaging_contract.py",
                                  "tests/test_persistence.py",
                                  "tests/test_persistence_concurrency.py",
                                  "tests/test_persistence_incremental.py",
                                  "tests/test_persistence_manager.py",
                                  "tests/test_persistence_worker_does_not_pin_its_manager.py",
                                  "tests/test_phi_retention.py",
                                  "tests/test_pixel_divergence.py",
                                  "tests/test_pixel_geometry_check.py",
                                  "tests/test_planar_configuration_roundtrip.py",
                                  "tests/test_private_tag_arity_roundtrip.py",
                                  "tests/test_private_tag_empty_value_roundtrip.py",
                                  "tests/test_private_tag_reload.py",
                                  "tests/test_private_tag_vr_roundtrip.py",
                                  "tests/test_save_all_contract.py",
                                  "tests/test_save_redact_race.py",
                                  "tests/test_save_reparenting.py",
                                  "tests/test_services.py",
                                  "tests/test_sidecar_gate_crosses_processes.py",
                                  "tests/test_sidecar_gate_order.py",
                                  "tests/test_study_date_roundtrip.py",
                                  "tests/test_vertical_table.py",
                                  "tests/test_worker_start_is_serialised.py",
                                  "tests/test_a_pre_097_store_keeps_one_offset_per_patient.py",
                                  "tests/test_the_project_secret_lives_in_the_store.py",
                                  "tests/test_a_restored_patient_is_saved.py",
                                  "tests/test_save_keeps_rows_memory_holds.py",
                                  # Beyond the scan, by a measured kill (#441's rule): deleting the
                                  # `_reparent_studies` call in `_save_patient` is killed by
                                  # test_restore_onto_an_id_a_raw_patient_holds_merges_them (#548, #551).
                                  "tests/test_patients_sharing_an_id_are_merged.py"], 30),
    # 587 sites (e184933). Until #414 the facade had no row, so no mutant
    # of `Session` -- the ingest/audit/anonymize/redact/export ordering,
    # `_make_lightweight_copy`, `_verify_worker`, the report's boundary
    # note -- was ever generated.
    #
    # Budget 30 is a stride of 19, the density io_handlers.py and
    # persistence.py are sampled at; a different number here would need a
    # reason.
    #
    # The list is measured, not curated: every file `_importers` in
    # tests/test_mutation_probe_targets.py demands, which is 131 of the
    # suite's 227 -- nearly all of it, because nearly every test drives
    # the facade. One full pass is ~350s under load (0e3e38c, 3.12.14;
    # ~300s unloaded at 4d34c64), so a surviving mutant costs six minutes
    # and a kill costs seconds. A coverage trace (#441) finds 12 more
    # files that execute this module without naming it; run against the
    # five survivors below they killed none, so the list gains nothing.
    #
    # Measured at budget 30 (0e3e38c, 3.12.14, loaded machine): 26 of 31
    # killed. #466 moved lines and left the 31 sampled mutants identical,
    # so the survivors are cited by code, not line. Each is classified
    # (#445):
    #   - `_match_ctp_rule`'s `r_man in eq_man` flipped to `not in`: a gap.
    #     No test handed the CTP matcher a rule whose manufacturer and
    #     model both match, so a matcher that never matches was green.
    #     Pinned since by tests/test_scaffold_features.py::
    #     test_a_ctp_rule_matches_on_manufacturer_and_model_containment,
    #     and killed by it on this row's list;
    #   - the suggested-config print's `sort_keys=False` flipped to True:
    #     equivalent. The printed block loads to the same mapping and only
    #     `action` moves ahead of `name`; test_suggested_config.py asserts
    #     the loaded mapping, and nothing documents the key order;
    #   - `_restart_executor`'s `cancel_futures=True` flipped: dead in
    #     production. The method has no caller in isocenter/, only the #220
    #     spawn-pin test in test_parallel_contract.py (#484);
    #   - the deleted `print("Persistence backend does not support
    #     compaction.")` in `compact()`'s `else`: unreachable, because
    #     `__init__` sets `store_backend` unconditionally and nothing
    #     removes it (#484);
    #   - the deleted `print(f"Load failed: {e}")` in `load_config`:
    #     equivalent for what anyone learns, since the ERROR log just above
    #     and the traceback print just below carry the same text. The
    #     silence there is that `load_config` swallows the failure and
    #     returns None (#456), which no print could fix.
    #
    # Off the budget-30 sample (4d34c64, 3.12.14), kept because they were
    # measured: at budget 3 all four sampled mutants are killed, among
    # them the deleted WARNING `reconcile_private_tags()` logs when it
    # drops stored rows -- a real gap until #414, now pinned by
    # tests/test_private_tag_reload.py. Deleting the `print(f"  {line}")`
    # that echoes each safe-export feedback line is killed by
    # test_safe_export_feedback.py. Deleting the INFO log "Batch preserved
    # identity for N patients" in the chunked identity-lock path survives
    # a full pass.
    #
    # Cost: at budget 30 this row is ~3.0 h of a default run as an upper
    # bound (31 mutants at ~350s each, loaded), and the measured run took
    # 85 minutes. Written down because an unexplained tripling of the run
    # time is the kind of thing someone later "fixes" by cutting the
    # budget.
    "isocenter/session.py": (["tests/test_analysis.py",
                              "tests/test_a_pre_096_store_is_not_reshifted.py",
                              "tests/test_a_replaced_study_date_is_raised.py",
                              "tests/test_an_unshifted_date_is_raised.py",
                              "tests/test_analysis_persistence.py",
                              "tests/test_api_coherence.py",
                              "tests/test_async_persistence.py",
                              "tests/test_automation.py",
                              "tests/test_binary_retention_threshold.py",
                              "tests/test_check_reversibility.py",
                              "tests/test_close_does_not_drop_an_orphaned_save.py",
                              "tests/test_close_warns_about_unsaved_instances.py",
                              "tests/test_colour_space_at_ingest.py",
                              "tests/test_compact_refuses_during_a_pass.py",
                              "tests/test_compact_rewiring_is_locked.py",
                              "tests/test_compaction.py",
                              "tests/test_compaction_races_a_concurrent_write.py",
                              "tests/test_compaction_recovery.py",
                              "tests/test_configuration_manual.py",
                              "tests/test_configuration_persistence.py",
                              "tests/test_dataframe_export.py",
                              "tests/test_date_shifted_roundtrip.py",
                              "tests/test_declined_date_recurs.py",
                              "tests/test_declined_remediation_is_recorded.py",
                              "tests/test_descriptor_edit_with_pixels_unloaded.py",
                              "tests/test_discovery_integration.py",
                              "tests/test_doc_anchors.py",
                              "tests/test_dtype_only_replacement_survives_the_dedup.py",
                              "tests/test_duplicate_sop_uid_at_ingest.py",
                              "tests/test_empty_sequence_roundtrip.py",
                              "tests/test_export_atomic_write.py",
                              "tests/test_export_bits_stored.py",
                              "tests/test_export_contract.py",
                              "tests/test_export_delivery_counters.py",
                              "tests/test_export_failure_audit.py",
                              "tests/test_export_flushes_before_it_sweeps.py",
                              "tests/test_export_loss_audit.py",
                              "tests/test_export_photometric_admissibility.py",
                              "tests/test_export_pixel_representation.py",
                              "tests/test_export_readback.py",
                              "tests/test_export_ybr_full_422.py",
                              "tests/test_feature_regression.py",
                              "tests/test_float_pixel_data_export.py",
                              "tests/test_floor_policy.py",
                              "tests/test_frozen_surface.py",
                              "tests/test_full_logging.py",
                              "tests/test_import_validation.py",
                              "tests/test_ingest_failure_audit.py",
                              "tests/test_ingest_imagecodecs_fallback.py",
                              "tests/test_ingestion_normalization.py",
                              "tests/test_io_no_pixels.py",
                              "tests/test_j2k_mct_label_on_export.py",
                              "tests/test_j2k_signedness_against_pixel_representation.py",
                              "tests/test_legacy_waveform_hydration.py",
                              "tests/test_load_config_raises.py",
                              "tests/test_lock_identities_signature.py",
                              "tests/test_logging.py",
                              "tests/test_manifest.py",
                              "tests/test_manifest_says_what_was_done.py",
                              "tests/test_memory_redaction.py",
                              "tests/test_memory_store_redaction_strategy.py",
                              "tests/test_memory_store_reports_its_processes_lever.py",
                              "tests/test_metadata_refactor_full.py",
                              "tests/test_missing_study_date.py",
                              "tests/test_multiprocessing.py",
                              "tests/test_murmur_annotations.py",
                              "tests/test_naming_structure.py",
                              "tests/test_nested_date_shifts_once.py",
                              "tests/test_nested_pixel_carriage.py",
                              "tests/test_ocr_leaves_frames_where_it_found_them.py",
                              "tests/test_ocr_unavailable_refuses.py",
                              "tests/test_ocr_workers_use_the_callers_tesseract.py",
                              "tests/test_offset_table_frame_count.py",
                              "tests/test_one_value_per_owned_tag.py",
                              "tests/test_optimization.py",
                              "tests/test_packaging_contract.py",
                              "tests/test_parallel_contract.py",
                              "tests/test_parallel_export.py",
                              "tests/test_patient_level_remediation_reaches_instances.py",
                              "tests/test_persistence.py",
                              "tests/test_persistence_manager.py",
                              "tests/test_phi_retention.py",
                              "tests/test_phi_status.py",
                              "tests/test_pixel_divergence.py",
                              "tests/test_pixel_dtype_roundtrip.py",
                              "tests/test_pixel_export.py",
                              "tests/test_pixel_geometry_check.py",
                              "tests/test_pixel_geometry_pipeline.py",
                              "tests/test_pixel_integrity.py",
                              "tests/test_private_binary_ingest.py",
                              "tests/test_private_tag_arity_roundtrip.py",
                              "tests/test_private_tag_empty_value_roundtrip.py",
                              "tests/test_private_tag_export.py",
                              "tests/test_private_tag_reload.py",
                              "tests/test_private_tag_vr_roundtrip.py",
                              "tests/test_profile_end_to_end.py",
                              "tests/test_query_export.py",
                              "tests/test_readback_label_admissibility.py",
                              "tests/test_redact_error.py",
                              "tests/test_redact_reports_outcome.py",
                              "tests/test_redaction_attestation.py",
                              "tests/test_redaction_consistency.py",
                              "tests/test_redaction_export.py",
                              "tests/test_redaction_failure_is_reported.py",
                              "tests/test_redaction_keeps_the_tag_scan_conclusion.py",
                              "tests/test_redaction_multizone.py",
                              "tests/test_redaction_names_its_strategy.py",
                              "tests/test_redaction_parallel.py",
                              "tests/test_redaction_reaches_the_exported_file.py",
                              "tests/test_redaction_uid_capture.py",
                              "tests/test_redaction_wildcard.py",
                              "tests/test_redaction_worker_count.py",
                              "tests/test_reingest_after_redact.py",
                              "tests/test_release_memory.py",
                              "tests/test_relock_identity_token.py",
                              "tests/test_remediation_actions.py",
                              "tests/test_report_action_evidence.py",
                              "tests/test_report_export_boundary.py",
                              "tests/test_report_section5_says_what_happened.py",
                              "tests/test_reporting_features.py",
                              "tests/test_reversibility.py",
                              "tests/test_safe_export.py",
                              "tests/test_safe_export_feedback.py",
                              "tests/test_safe_export_jitter.py",
                              "tests/test_save_all_contract.py",
                              "tests/test_save_redact_race.py",
                              "tests/test_scaffold_features.py",
                              "tests/test_scaffold_profiles.py",
                              "tests/test_scaffolding.py",
                              "tests/test_scan_failures_are_audited.py",
                              "tests/test_scan_pixel_content_dispatches_its_worker.py",
                              "tests/test_scan_pixel_findings_name_the_live_graph.py",
                              "tests/test_scan_reports_what_it_could_not_read.py",
                              "tests/test_session.py",
                              "tests/test_shared_executor_lifecycle.py",
                              "tests/test_shipped_resource_is_required.py",
                              "tests/test_sidecar.py",
                              "tests/test_sidecar_gate_order.py",
                              "tests/test_signed_lossless_jpeg_decode.py",
                              "tests/test_signed_pixels_survive_a_compressed_export.py",
                              "tests/test_study_date_roundtrip.py",
                              "tests/test_suggested_config.py",
                              "tests/test_sync_save_does_not_overlap_an_async_one.py",
                              "tests/test_the_jitter_seed_survives_anonymize.py",
                              "tests/test_waveform_dicom_roundtrip.py",
                              "tests/test_waveform_ingest.py",
                              "tests/test_wfdb_conformance.py",
                              "tests/test_wfdb_option_strictness.py",
                              "tests/test_wfdb_partial_export_is_audited.py",
                              "tests/test_wfdb_privacy.py",
                              "tests/test_wfdb_start_date_honesty.py",
                              "tests/test_wfdb_writer.py",
                              "tests/test_worker_loss_is_reported.py",
                              "tests/test_ybr_jpegls_read_doors.py",
                              "tests/test_ybr_read_door_labels.py",
                              "tests/test_a_pre_097_store_keeps_one_offset_per_patient.py",
                              "tests/test_the_project_secret_lives_in_the_store.py",
                              "tests/test_nested_remediation_reaches_the_instance.py",
                              # Beyond the scan, by measured kills (#441's rule): deleting either
                              # `_merge_patients_sharing_an_id` call is killed by
                              # test_patients_sharing_an_id_are_merged.py (#548), and deleting
                              # the restore's `p.mark_modified()` by
                              # test_a_restored_patient_is_saved.py (#552).
                              "tests/test_a_restored_patient_is_saved.py",
                              "tests/test_patients_sharing_an_id_are_merged.py"],
                             30),
    # 196 sites. Until #419 this module had no row, so the persistence
    # bookkeeping every CLAUDE.md trap is about -- `mark_modified`,
    # `mark_persisted`'s `max`, `phi_status`'s revision comparison,
    # `record_phi_status`'s short-circuit -- and `unload_pixel_data()`'s
    # #293 refusal were never mutated.
    #
    # The sample does not reach that bookkeeping, and no budget here
    # would make it: budget 30 is a stride of 6, sites 0, 6, 12, ..., and
    # of the bookkeeping mutants only site 12 -- `phi_status`'s `is None`
    # flipped to `is not None` -- is generated by default. The
    # `mark_persisted` flip, `phi_status`'s `!=` flip and its `or -> and`,
    # and `record_phi_status`'s short-circuit flip never are. What pins
    # the bookkeeping is the tests named after it (test_phi_status.py and
    # the #173/#307 pins in test_remediation_invariants.py), not this
    # row's sample.
    #
    # So the budget is a cost decision: 30 is ~1.8 h of upper bound for
    # 33 mutants, 12 would be ~0.74 h for 13. It stays at 30, because the
    # classification below is of exactly this stride-6 sample. A stride
    # of 16 shares five of its sites (0, 48, 96, 144, 192), so a default
    # run at 12 would print survivors nobody has read -- the cost this row
    # exists to avoid -- to save an hour on a tool nothing in CI runs.
    #
    # The list is measured, not curated (125 files, the `_importers`
    # demand), and here completeness was shown to matter: flipping the
    # `or` in `phi_status` to `and` passes the six files that pin the
    # bookkeeping by name and is killed only by tests/test_phi_status.py
    # in the full list (two of its tests go red: the edit-after-a-scan
    # invalidation and the stale status that must not persist as
    # current). A curated list would have reported a false survivor. One full pass is ~190s (3.12.14).
    #
    # Measured at budget 30 (4d34c64, 3.12.14): 25 of 33 killed. The
    # eight survivors, each classified:
    #   - four `@dataclass(...)` / `field(...)` keyword flips (`slots`
    #     twice, and `repr=False` twice): layout and repr, nothing any
    #     test or caller reads -- equivalent. None is a `frozen` flip,
    #     which would not be: `Equipment` is frozen so value-hashing works;
    #   - the `ds is not None and hasattr(ds, "file_meta")` flip in the
    #     pixel fallback, which only builds the transfer-syntax UID quoted
    #     in an error message -- equivalent;
    #   - `_write_int_if_changed`'s `return True` flipped, whose result only
    #     decides whether a DEBUG "BitsAllocated ... corrected" line is
    #     logged: a DEBUG log with no reader and no contract -- equivalent;
    #   - `_write_str_if_changed`'s `return False` -> None, whose only
    #     caller discards the result -- equivalent;
    #   - `unload_waveform_data()`'s `return True` for samples that are
    #     already absent, flipped to False. NOT classed equivalent: a caller
    #     told False believes the samples could not be released. Pinned
    #     since by `tests/test_waveform_ingest.py::
    #     test_already_absent_samples_report_as_released` (#443).
    # At budget 3, deleting the DEBUG "Identity regenerated" log also
    # survives: no reader, no contract -- equivalent.
    #
    # Cost: ~1.8 h of a default run as an upper bound.
    "isocenter/entities.py": (["tests/test_analysis.py",
                               "tests/test_a_pre_096_store_is_not_reshifted.py",
                               "tests/test_a_replaced_study_date_is_raised.py",
                               "tests/test_an_unshifted_date_is_raised.py",
                               "tests/test_api_coherence.py",
                               "tests/test_async_persistence.py",
                               "tests/test_audit_suppression.py",
                               "tests/test_blob_storage.py",
                               "tests/test_bytes_persistence.py",
                               "tests/test_check_reversibility.py",
                               "tests/test_close_does_not_drop_an_orphaned_save.py",
                               "tests/test_close_warns_about_unsaved_instances.py",
                               "tests/test_codecs_strict.py",
                               "tests/test_colour_space_at_ingest.py",
                               "tests/test_compact_refuses_during_a_pass.py",
                               "tests/test_compact_rewiring_is_locked.py",
                               "tests/test_compaction.py",
                               "tests/test_compaction_races_a_concurrent_write.py",
                               "tests/test_compaction_reclaims_a_row_instances_does_not_carry.py",
                               "tests/test_compaction_recovery.py",
                               "tests/test_compression_deps.py",
                               "tests/test_concurrency_stress.py",
                               "tests/test_config_tags_shapes.py",
                               "tests/test_create_config_output.py",
                               "tests/test_dataframe_export.py",
                               "tests/test_declined_date_recurs.py",
                               "tests/test_declined_remediation_is_recorded.py",
                               "tests/test_deid_tags.py",
                               "tests/test_descriptor_edit_with_pixels_unloaded.py",
                               "tests/test_empty_sequence_roundtrip.py",
                               "tests/test_entities.py",
                               "tests/test_entity_state_vocabulary.py",
                               "tests/test_export_atomic_write.py",
                               "tests/test_export_bits_stored.py",
                               "tests/test_export_contract.py",
                               "tests/test_export_date_error.py",
                               "tests/test_export_delivery_counters.py",
                               "tests/test_export_error.py",
                               "tests/test_export_failure_audit.py",
                               "tests/test_export_flushes_before_it_sweeps.py",
                               "tests/test_export_loss_audit.py",
                               "tests/test_export_merge_shape.py",
                               "tests/test_export_photometric_admissibility.py",
                               "tests/test_export_pixel_representation.py",
                               "tests/test_export_pixels.py",
                               "tests/test_export_readback.py",
                               "tests/test_export_worker_graph_purity.py",
                               "tests/test_export_ybr_full_422.py",
                               "tests/test_float_pixel_data_export.py",
                               "tests/test_floor_policy.py",
                               "tests/test_flush_orphan_recovery.py",
                               "tests/test_frozen_surface.py",
                               "tests/test_ingest_imagecodecs_fallback.py",
                               "tests/test_io.py",
                               "tests/test_io_no_pixels.py",
                               "tests/test_j2k_signedness_against_pixel_representation.py",
                               "tests/test_legacy_waveform_hydration.py",
                               "tests/test_lock_identities_signature.py",
                               "tests/test_manifest_says_what_was_done.py",
                               "tests/test_memory_redaction.py",
                               "tests/test_memory_store_redaction_strategy.py",
                               "tests/test_memory_store_reports_its_processes_lever.py",
                               "tests/test_murmur_annotations.py",
                               "tests/test_mutation_gaps.py",
                               "tests/test_nested_date_shifts_once.py",
                               "tests/test_nested_phi_audit.py",
                               "tests/test_ocr_formal.py",
                               "tests/test_ocr_leaves_frames_where_it_found_them.py",
                               "tests/test_ocr_unavailable_refuses.py",
                               "tests/test_ocr_workers_use_the_callers_tesseract.py",
                               "tests/test_offset_table_frame_count.py",
                               "tests/test_one_value_per_owned_tag.py",
                               "tests/test_optimization.py",
                               "tests/test_parallel_contract.py",
                               "tests/test_parallel_export.py",
                               "tests/test_patient_level_remediation_reaches_instances.py",
                               "tests/test_persistence.py",
                               "tests/test_persistence_concurrency.py",
                               "tests/test_persistence_incremental.py",
                               "tests/test_persistence_manager.py",
                               "tests/test_phi_retention.py",
                               "tests/test_phi_status.py",
                               "tests/test_pixel_analysis.py",
                               "tests/test_pixel_divergence.py",
                               "tests/test_pixel_dtype_roundtrip.py",
                               "tests/test_pixel_geometry_check.py",
                               "tests/test_pixel_geometry_pipeline.py",
                               "tests/test_planar_configuration_roundtrip.py",
                               "tests/test_privacy.py",
                               "tests/test_private_binary_ingest.py",
                               "tests/test_private_tag_reload.py",
                               "tests/test_pydicom_deprecations.py",
                               "tests/test_query_export.py",
                               "tests/test_readback_label_admissibility.py",
                               "tests/test_recursive_import.py",
                               "tests/test_redact_error.py",
                               "tests/test_redact_reports_outcome.py",
                               "tests/test_redaction_consistency.py",
                               "tests/test_redaction_failure_is_reported.py",
                               "tests/test_redaction_keeps_the_tag_scan_conclusion.py",
                               "tests/test_redaction_multizone.py",
                               "tests/test_redaction_names_its_strategy.py",
                               "tests/test_redaction_optimization.py",
                               "tests/test_redaction_parallel.py",
                               "tests/test_redaction_rgb.py",
                               "tests/test_redaction_robustness.py",
                               "tests/test_redaction_roi.py",
                               "tests/test_reingest_after_redact.py",
                               "tests/test_release_memory.py",
                               "tests/test_relock_identity_token.py",
                               "tests/test_remediation.py",
                               "tests/test_remediation_accounting.py",
                               "tests/test_remediation_actions.py",
                               "tests/test_remediation_invariants.py",
                               "tests/test_report_section5_says_what_happened.py",
                               "tests/test_reporting_features.py",
                               "tests/test_reversibility.py",
                               "tests/test_reversibility_coverage.py",
                               "tests/test_safe_export.py",
                               "tests/test_safe_export_jitter.py",
                               "tests/test_save_all_contract.py",
                               "tests/test_save_redact_race.py",
                               "tests/test_save_reparenting.py",
                               "tests/test_scaffold_features.py",
                               "tests/test_scaffolding.py",
                               "tests/test_scan_failures_are_audited.py",
                               "tests/test_scan_pixel_content_dispatches_its_worker.py",
                               "tests/test_scan_pixel_findings_name_the_live_graph.py",
                               "tests/test_scan_reports_what_it_could_not_read.py",
                               "tests/test_sidecar.py",
                               "tests/test_sidecar_gate_crosses_processes.py",
                               "tests/test_sidecar_gate_order.py",
                               "tests/test_signed_lossless_jpeg_decode.py",
                               "tests/test_single_frame_encapsulated_decode.py",
                               "tests/test_sr_anonymization.py",
                               "tests/test_structured_export.py",
                               "tests/test_study_date_roundtrip.py",
                               "tests/test_sync_save_does_not_overlap_an_async_one.py",
                               "tests/test_tag_key_normalisation.py",
                               "tests/test_the_jitter_seed_survives_anonymize.py",
                               "tests/test_uid_regeneration.py",
                               "tests/test_verification_logic.py",
                               "tests/test_voi_lut_integration.py",
                               "tests/test_waveform_dicom_roundtrip.py",
                               "tests/test_waveform_ingest.py",
                               "tests/test_waveform_model.py",
                               "tests/test_wfdb_conformance.py",
                               "tests/test_wfdb_start_date_honesty.py",
                               "tests/test_wfdb_writer.py",
                               "tests/test_worker_loss_is_reported.py",
                               "tests/test_worker_start_is_serialised.py",
                               "tests/test_ybr_jpegls_read_doors.py",
                               "tests/test_ybr_read_door_labels.py",
                               "tests/test_a_pre_097_store_keeps_one_offset_per_patient.py",
                               "tests/test_the_project_secret_keys_the_pseudonym_and_offset.py",
                               "tests/test_the_project_secret_lives_in_the_store.py",
                               "tests/test_a_restored_patient_is_saved.py",
                               "tests/test_nested_remediation_reaches_the_instance.py",
                               "tests/test_patients_sharing_an_id_are_merged.py",
                               "tests/test_save_keeps_rows_memory_holds.py"],
                              30),
    # 81 sites; budget 60 is stride 1 (81 // 60), so every site is
    # probed, exhaustive because it is cheap, like parallel.py's 80. It
    # stays stride 1 until the module passes 119 sites.
    #
    # Eight files -- the seventh, test_ybr_jpegls_read_doors.py, is
    # #483's, and the eighth, test_ybr_read_door_labels.py, is #482's --
    # and it takes the widened `_importers` (#419) to see them: several
    # reach this module as `from isocenter import
    # imagecodecs_handler`, which the old stem-only scan could not read.
    # Without test_offset_table_frame_count.py, nine of the #418
    # frame-count helpers' mutants survive; without
    # test_signed_lossless_jpeg_decode.py, the #446 sign rule and the
    # lj92 pad have no witness at all.
    #
    # Measured at stride 1 on the #446/#447 branch (3.12.14): 54 of 61
    # killed, and a seventh (the sign rule's `or` -> `and` in
    # `_sign_extend_from_bits_stored`) was then pinned by that file's S6
    # and killed by a real edit. Re-measured at stride 1 once #440 deleted
    # two dead handler hooks (3.12.14): 58 of 60 killed. #483 then added
    # 19 sites (`CONVERTS_TO`, `colour_conversion`, `convert_colour`,
    # `_jpegls_precision`; #478, #464) and the seventh file. Re-measured
    # at stride 1 on ba804ae (3.12.14, 28.6s control, other suites on the
    # machine): 77 of 79 killed, every one of the 19 new sites among them,
    # no timeout. The two survivors are the same two, both known and
    # both equivalent:
    #   - the decode-error print in `get_pixel_data` is equivalent: the
    #     exception it describes is re-raised carrying the same text;
    #   - the print in `is_available()` is equivalent now. It was the only
    #     place the import failure's cause reached anyone until #444 put
    #     the cause in the raise itself.
    # #482 then added 2 sites, taking the module to 81. Every other site
    # is the one #489 measured: the operator kinds are unchanged and only
    # line numbers moved, so the 77 above carries. The two new ones are
    # NOT the table -- `DECODER_RELABELS` is a dict literal, which no
    # operator in this probe can see. They are the `or ""` in the lookup
    # that reads it (`Or -> And`) and the `is not None` guard on the
    # relabel (`IsNot -> Is`). Both measured here at stride 1 on this
    # branch (3.12.14, 27.6s control against #489's 28.6s), both KILLED,
    # by tests/test_ybr_read_door_labels.py. That is 79 of 81, with the
    # same two known-equivalent survivors named above and no new one.
    # The RLE arm, which no mutant could reach through a real decode, is
    # gone (#447).
    "isocenter/imagecodecs_handler.py": (["tests/test_codecs_strict.py",
                                          "tests/test_imagecodecs_edge_cases.py",
                                          "tests/test_ingest_imagecodecs_fallback.py",
                                          "tests/test_j2k_signedness_against_pixel_representation.py",
                                          "tests/test_offset_table_frame_count.py",
                                          "tests/test_signed_lossless_jpeg_decode.py",
                                          "tests/test_single_frame_encapsulated_decode.py",
                                          "tests/test_ybr_jpegls_read_doors.py",
                                          "tests/test_ybr_read_door_labels.py"],
                                         60),
}

# Every module under `isocenter/` that has no TARGETS row, and why. Rowed
# or listed here, never neither and never both:
# tests/test_mutation_probe_targets.py fails otherwise, so a module that
# grows into behaviour cannot go unprobed silently again -- which is how
# persistence.py (#383), session.py (#414), entities.py and
# imagecodecs_handler.py (#419) each spent releases without a row, found
# each time by a person noticing rather than by a check.
#
# It lives here rather than in the test so the person running the probe
# sees what it does not measure: a default run prints it last, because the
# tail of a multi-hour run is what gets read.
#
# #439 rowed eleven of the 24 modules this ledger first deferred; the
# 13 still deferred are below. Two of their obstacles are named by
# issue: reach by class name or format string, which no import scan sees
# (#441, settled by the rule in the comment above TARGETS: a row carries
# such a file only with a measured kill), and the flat 900s per-mutant
# timeout a hang cost, which is fixed -- a mutant's limit is now derived
# from its control (#442).
#
# A reason that starts "0 sites" is recomputed by that test and must stay
# true. The other numbers are dated notes, not checked -- an edit that
# moves them does not make the reason wrong. Sites (`count_ops`) and
# importers (the test's `_importers`) are counted at e184933. Survivor
# counts and seconds per pass (one pass of those importers) were measured
# at 0e3e38c on 3.12.14, at budget 30 unless an entry says otherwise, on
# a machine running other suites: controls ran 1.2-1.8x the unloaded
# 4d34c64 figures, so read the seconds as upper-side.
NOT_PROBED = {
    # Permanently excluded.
    "isocenter/__init__.py":
        "1 site, but 217 of 221 test files import the package, so its row "
        "would run the whole suite for each mutant: a full-suite probe of "
        "one re-export, not a measurement",
    "isocenter/_version.py": "0 sites: a version string",
    "isocenter/utils/__init__.py": "0 sites: an empty package marker",
    "isocenter/profiles.py":
        "0 sites: data only, the shipped profile tables; what reads them "
        "is probed where it lives",

    # Deferred: the scan demands almost nothing, and the reach is by class
    # name or through the facade. A row needs a hand list, and #441's rule
    # admits a file only with a measured kill, so a mutant run over the
    # files that execute the module comes first.
    "isocenter/store.py":
        "deferred: 16 sites, 1 importer (test_shared_executor_lifecycle.py), "
        "but 127 test files execute DicomStore through the facade (#441); "
        "a hand list needs a mutant run over them first, ~80 minutes at "
        "session-sized passes",
    "isocenter/logger.py":
        "deferred: 14 sites, 2 importers -- reached through get_logger() "
        "and describe_exception(), whose spelling "
        "tests/test_ingest_failure_audit.py pins directly (#435); a row "
        "would still need its list written by hand",

    # Deferred: at budget 30 a default run would print survivors nobody
    # has read, or sample a mutant that never terminates. Rowing one means
    # classifying its survivors first, as the eleven #439 rowed were.
    "isocenter/services.py":
        "deferred: 151 sites, 23 importers, 47.3s per pass; a sample of 9 "
        "killed 6, and budget 30 is 31 mutants at that pass cost",
    "isocenter/persistence_manager.py":
        "deferred: 99 sites, 7 importers, 22.8s per pass; a sample of 9 "
        "killed only 2, and 126 test files execute it that the scan does "
        "not name (#441), so its survivors may be phantoms",
    "isocenter/exporters/wfdb.py":
        "deferred: 97 sites, 6 importers, 16.5s per pass; killed 28/32 at "
        "stride 3, four survivors unclassified, and site 45 -- `while "
        "candidate in seen:` flipped to `not in` -- never terminates, so a "
        "row would report it as a TIMEOUT once mutant_timeout() expires, "
        "three times the control (about 50s here; #442)",
    "isocenter/waveform.py":
        "deferred: 69 sites, 5 importers, 12.1s per pass; killed 30/35 at "
        "stride 2, five survivors unclassified",
    "isocenter/pixel_analysis.py":
        "deferred: 63 sites, 12 importers, 20.0s per pass; killed 26/33 at "
        "stride 2 on its 65 sites before #466 moved them, seven survivors "
        "unclassified",
    "isocenter/sidecar.py":
        "deferred: 19 sites, 6 importers, 20.5s per pass; killed 14/19 on "
        "its then 5 importers -- the survivors are both flocks, a flush, "
        "an fsync and the decompressor's flush(), durability and "
        "cross-process locking, unclassified",
    "isocenter/utils/ctp_parser.py":
        "deferred: 22 sites, 2 importers, 1.3s per pass; killed 15/22 -- "
        "four sys.exit or print deletions in the CLI main(), two yaml "
        "flags and `if not criteria`, unclassified",
    "isocenter/murmur.py":
        "deferred: 52 sites, 1 importer, 2.6s per pass; killed 42/52, ten "
        "survivors unclassified",
    "isocenter/discovery.py":
        "deferred: 77 sites, 5 importers, 0.9s per pass; a budget-30 "
        "sample (stride 2, 39 mutants) killed 28 and left ten survivors "
        "unclassified. Flipping the BFS's `visited[neighbor] = True` to "
        "False never terminates; it is caught in 2s today only because -x "
        "stops on test_merge_disjoint first, and would cost "
        "mutant_timeout()'s 30s floor if the order changed (#442). The one "
        "sampled mutant that timed out is another: site 60, `if not "
        "visited[neighbor]:` with the `not` dropped, which does not "
        "terminate even under -x and is a TIMEOUT at that 30s floor "
        "(0e3e38c)",
}

class Mut(ast.NodeTransformer):
    """Applies exactly the nth mutation opportunity found."""
    def __init__(self, target): self.target, self.n, self.desc = target, 0, None
    def _hit(self):
        self.n += 1
        return self.n - 1 == self.target
    def visit_Compare(self, node):
        self.generic_visit(node)
        flip = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.Gt: ast.LtE,
                ast.LtE: ast.Gt, ast.GtE: ast.Lt, ast.In: ast.NotIn, ast.NotIn: ast.In,
                ast.Is: ast.IsNot, ast.IsNot: ast.Is}
        if len(node.ops) == 1 and type(node.ops[0]) in flip:
            if self._hit():
                new = flip[type(node.ops[0])]
                self.desc = f"line {node.lineno}: {type(node.ops[0]).__name__} -> {new.__name__}"
                node.ops = [new()]
        return node
    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self._hit():
            new = ast.Or if isinstance(node.op, ast.And) else ast.And
            self.desc = f"line {node.lineno}: {type(node.op).__name__} -> {new.__name__}"
            node.op = new()
        return node
    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            if self._hit():
                self.desc = f"line {node.lineno}: {node.value} -> {not node.value}"
                return ast.copy_location(ast.Constant(value=not node.value), node)
        return node

    # The three operators above only see *decisions*. A module of
    # straight-line calls has none, so it reported 0 sites and 0
    # survivors -- which reads like a clean bill of health next to
    # `privacy.py 11/36` and actually meant "not measured" (#106).
    # crypto.py, the reversible-anonymisation core, was in that state.
    # The three below reach code that decides nothing.
    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit():
            self.desc = f"line {node.lineno}: dropped `not`"
            return node.operand
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        already_none = (isinstance(node.value, ast.Constant)
                        and node.value.value is None)
        if node.value is not None and not already_none and self._hit():
            self.desc = f"line {node.lineno}: return <value> -> return None"
            return ast.copy_location(ast.Return(value=ast.Constant(value=None)), node)
        return node

    def visit_Expr(self, node):
        # A bare expression statement is there for its side effect, so
        # dropping it is the cheapest way to ask whether anything checks
        # that the side effect happened. Becomes `pass` rather than being
        # deleted: removing the only statement in a body leaves an AST
        # that will not unparse, and the failure would look like a
        # skipped mutant rather than a bug in this file.
        if isinstance(node.value, ast.Constant):
            return node  # a docstring; deleting it is an equivalent mutant
        self.generic_visit(node)
        if self._hit():
            self.desc = f"line {node.lineno}: deleted statement"
            return ast.copy_location(ast.Pass(), node)
        return node

def count_ops(src):
    n = 0
    while True:
        m = Mut(n); m.visit(ast.parse(src))
        if m.n <= n: return n
        n += 1

#: Answers from `subprocess_cache_path`, keyed on (interpreter, source
#: path). Per *path*, not per cache tag: the subprocess honours
#: `PYTHONPYCACHEPREFIX`/`sys.pycache_prefix`, so two sources under one
#: tag can cache into different directories and a tag-keyed memo would
#: hand one module the other's answer.
_SUBPROCESS_CACHE_PATHS = {}

def subprocess_cache_path(path):
    """The `__pycache__` entry `PYTEST[0]` would read for `path`.

    Asked of that interpreter, never derived here: a local
    `cache_from_source` names the file from the *parent's*
    `sys.implementation.cache_tag`, and the parent is routinely not the
    interpreter that runs the tests -- a pyenv shim beside the hardcoded
    `.venv` is the ordinary case, not the exotic one. Inspecting the
    parent-tag file degrades one direction only, never a false abort and
    always a false pass, so nothing about running the probe would ever
    say the guard had gone quiet (#201).

    The full path is requested rather than just the tag because the
    subprocess honours `PYTHONPYCACHEPREFIX`: a hand-assembled
    `<dir>/__pycache__/<stem>.<tag>.pyc` looks in the wrong directory
    under a cache prefix. One queried path covers every execution lever
    the run has -- `PYTEST[0]` is the process that imports the mutant,
    threads share it, and process workers spawn from the same
    `sys.executable` (so the same tag, with `PYTHONDONTWRITEBYTECODE`
    inherited either way).

    An interpreter that is missing or cannot answer is a `SystemExit`
    naming it, not a guess: the probe prefers aborting over reporting,
    and the raw FileNotFoundError this replaces was how a worktree
    without a `.venv` used to die mid-run.
    """
    resolved = Path(path).resolve()
    key = (PYTEST[0], str(resolved))
    if key not in _SUBPROCESS_CACHE_PATHS:
        query = ("import importlib.util, sys; "
                 "print(importlib.util.cache_from_source(sys.argv[1]))")
        try:
            r = subprocess.run([PYTEST[0], "-c", query, str(resolved)],
                               capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise SystemExit(
                f"ABORT: the test interpreter {PYTEST[0]} does not exist, so "
                f"no verdict it produced could be vouched for. Create the "
                f".venv (pip install -e '.[dev]') or point PYTEST at the "
                f"interpreter that should run the tests.") from None
        answer = r.stdout.strip()
        if r.returncode != 0 or not answer:
            raise SystemExit(
                f"ABORT: the test interpreter {PYTEST[0]} could not name its "
                f"bytecode cache for {resolved} (exit {r.returncode}: "
                f"{r.stderr.strip() or 'no output'}). A guard pointed at a "
                f"guessed path is #201's silent no-op again, so the probe "
                f"stops instead.")
        _SUBPROCESS_CACHE_PATHS[key] = Path(answer)
    return _SUBPROCESS_CACHE_PATHS[key]

def assert_fresh(path, cache):
    """Stop the run if a cached `.pyc` would be reused for what was just written.

    CPython validates a timestamp-based `.pyc` against the source's
    `(mtime, size)` pair, with the mtime truncated to whole seconds.
    Both halves collide far more easily here than they look.

    *Size* collides by construction. The probe writes `ast.unparse`
    output, so consecutive mutants differ from each other only by the
    mutation delta -- and two `Eq -> NotEq` flips, two dropped `not`s,
    or two `True -> False`s differ by exactly zero bytes. Comparison
    flips dominate most modules, so equal-size neighbours are the norm,
    not the exception.

    *Seconds* collide whenever a run is quick. `crypto.py`'s tests take
    0.6s, so consecutive writes land in the same second about half the
    time; the 15s runs on `privacy.py` are what makes this intermittent
    rather than constant.

    When both match, the interpreter hands back the *previous* mutant's
    bytecode and pytest never sees the mutation being scored. The verdict
    is then about code that was not there. It is not biased toward
    survivors either: it repeats the neighbour's verdict, so it invents
    a coverage gap or hides one depending on which way the neighbour
    went (#174).

    `PYTHONDONTWRITEBYTECODE=1` in `run()` is the fix -- not because it
    stops a `.pyc` being *read* (it does not) but because it stops each
    run planting the trap the next one falls into. A cache left behind by
    something else cannot spring it: its recorded mtime is in the past
    and the probe's writes are always later.

    This asserts that rather than trusting it, and aborts instead of
    printing a verdict. A probe that cannot tell "the suite did not
    notice" from "the suite was never shown" is exactly the silent
    failure it exists to hunt for.

    `cache` is required, with no default, on purpose. The cache that
    matters belongs to the interpreter that RUNS the tests -- callers
    pass `subprocess_cache_path(path)` -- and this function must never
    derive one itself: a `cache_from_source` fallback resurrects the
    parent interpreter's tag, and a pyenv shim launching the probe
    beside the hardcoded `.venv` is the routine case, not the edge. A
    guard built that way inspects a `.pyc` the subprocess never reads,
    finds nothing, and returns -- never a false abort, always a false
    pass, so no run would ever reveal the check had been off (#201).
    """
    cache = Path(cache)
    if not cache.exists():
        return
    head = cache.read_bytes()[:16]
    if len(head) < 16:
        return
    # Two deliberate divergences from `_validate_timestamp_pyc`: the
    # `& 0xFFFFFFFF` masking on both halves is omitted, which matters in
    # 2106 or on a 4GB source, and the magic number CPython checks first
    # is not, which matters only across a pre-release magic bump inside
    # one cache tag. Neither can reach this script.
    flags, mtime, size = struct.unpack("<III", head[4:16])
    if flags & 0b1:
        # Hash-based `.pyc`. CHECKED_HASH is verified against the source's
        # own hash and cannot go stale; UNCHECKED_HASH is trusted blind,
        # which is worse than the timestamp case, not better.
        if flags & 0b10:
            return
        why = "an UNCHECKED_HASH .pyc is reused without looking at the source"
    else:
        st = path.stat()
        if not (mtime == int(st.st_mtime) and size == st.st_size):
            return
        why = (f"its recorded (mtime={mtime}, size={size}) matches the file "
               f"just written")
    raise SystemExit(
        f"ABORT: {cache} is stale bytecode that CPython would reuse -- {why}. "
        f"pytest would execute the previous mutant and the verdict would not "
        f"be about this mutation. See assert_fresh() and #174.")

#: A mutant's limit is this many control passes (#442). The control is the
#: same test list over the unmutated module, so 3x is room for a mutant that
#: legitimately slows the suite, and for load: the controls behind the
#: factor were measured while other pytest runs shared the machine. At 3x,
#: session.py's ~310s control gets ~930s -- the flat 900s it replaced, to
#: within 3% -- and every faster row gets its hang cost cut in proportion.
MUTANT_TIMEOUT_FACTOR = 3

#: The floor under that. Without it a 1s control (crypto.py, discovery.py)
#: gives a 3s limit, and a loaded machine then times out healthy mutants
#: and scores them as detected -- a kill nobody earned.
MUTANT_TIMEOUT_FLOOR_S = 30

#: The control's own limit, about 6x the slowest control measured
#: (session.py, ~310s). Deliberately not derived: the control is what sets
#: every mutant's limit, so it is the one run that must not time out
#: because the machine was busy.
CONTROL_TIMEOUT_S = 1800

#: The clock `main()` times the control with. A module-level name so a test
#: can replace this one binding, rather than the process-wide
#: `time.monotonic` pytest reads too.
_clock = time.monotonic


def mutant_timeout(control_s):
    """How long one mutant may run: `max(FLOOR, FACTOR * control_s)` (#442).

    It was a flat 900s for every module, so a mutant that stopped the tests
    terminating cost fifteen minutes even where the whole list passes in
    one second.
    """
    return max(MUTANT_TIMEOUT_FLOOR_S, MUTANT_TIMEOUT_FACTOR * control_s)


def run(tests, timeout):
    # `timeout` has no default on purpose: a default is the flat 900s this
    # replaced sneaking back in through a caller that forgot it (#442).
    #
    # PYTHONDONTWRITEBYTECODE rather than `-B`: `run_parallel()` spawns
    # worker processes, and a grandchild that writes a `.pyc` plants the
    # same trap the parent avoided. The variable is inherited
    # unconditionally; an interpreter flag reaches a spawned child only
    # through `_args_from_interpreter_flags`, which is not a promise this
    # script should rest on. `os.environ` is copied, not replaced -- a bare
    # `env=` dict loses PATH and the failure looks like a killed mutant.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # A session of its own, and the group KILL on a timeout (#476).
    # `subprocess.run(timeout=)` kills the direct child only, and a mutant
    # that spins inside a `run_parallel()` worker leaves that worker
    # running after pytest is gone: reparented to init, at about 77% CPU
    # in the PR #480 review, and inside every later mutant's timing. The
    # workers inherit pytest's process group, and `start_new_session`
    # makes that group pytest's alone, so the KILL reaches them and
    # nothing else.
    with subprocess.Popen(PYTEST + tests, cwd=REPO, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env,
                          start_new_session=True) as proc:
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            raise
    return proc.returncode == 0

def main():
    argv = sys.argv[1:]
    override = int(argv[0]) if argv and argv[0].isdigit() else None
    rest = argv[1:] if argv and argv[0].isdigit() else argv
    if len(rest) >= 2:
        targets = {rest[0]: (list(rest[1:]), override if override is not None else 30)}
    else:
        targets = TARGETS

    for mod, (tests, budget) in targets.items():
        if override is not None:
            budget = override
        path = REPO / mod
        original = path.read_text()
        total = count_ops(original)

        # Control: unparsed-but-unmutated must still pass, or every
        # result below is an artefact of the harness rather than a finding.
        path.write_text(ast.unparse(ast.parse(original)))
        timed_out = False
        try:
            assert_fresh(path, subprocess_cache_path(path))
            t0 = _clock()
            ok = run(tests, CONTROL_TIMEOUT_S)
            control_s = _clock() - t0
        except subprocess.TimeoutExpired:
            # One module's results are unusable; the run is not. Before
            # #442 this escaped main() and ended a multi-hour run at the
            # first slow control, printing nothing for the modules after it.
            ok, timed_out = False, True
        finally:
            path.write_text(original)
        print(f"\n### {mod}  ({total} mutation sites, sampling {budget})")
        if timed_out:
            print(f"    control (unparsed, unmutated): TIMEOUT after "
                  f"{CONTROL_TIMEOUT_S}s -- results unusable")
            continue
        print(f"    control (unparsed, unmutated): {'PASS' if ok else 'FAIL -- results unusable'}")
        if not ok:
            continue
        # A module with no sites was NOT MEASURED. Left to speak for
        # itself, "0 survived" sits in a table next to "11/36" and reads
        # as the healthiest row (#106).
        if total == 0:
            print("    => NOT MEASURED: no operator in this probe can see "
                  "this module. This is not a clean result -- it is the "
                  "absence of one. Add an operator that reaches it.")
            continue

        step = max(1, total // budget)
        limit = mutant_timeout(control_s)
        survived, killed, by_timeout = [], 0, 0
        for i in range(0, total, step):
            m = Mut(i); tree = m.visit(ast.parse(original))
            if m.desc is None: continue
            try:
                path.write_text(ast.unparse(ast.fix_missing_locations(tree)))
                # Not caught by the `except Exception` below on purpose: a
                # stale cache invalidates the whole run, not one sample.
                # (`subprocess_cache_path`'s aborts ride the same exit.)
                assert_fresh(path, subprocess_cache_path(path))
                t0 = time.time()
                if run(tests, limit):
                    survived.append(m.desc)
                    print(f"    SURVIVED  {m.desc}  ({time.time()-t0:.0f}s)")
                else:
                    killed += 1
            # Before `except Exception`, which would call it `skipped` and
            # leave it out of `n` -- how a hang was scored until #442. The
            # tests did notice this mutant: they stopped finishing. So it
            # is a kill (owner ruling, #442 Q3), counted separately so a
            # row of timeouts is not read as a row of red tests.
            except subprocess.TimeoutExpired:
                killed += 1
                by_timeout += 1
                print(f"    TIMEOUT   {m.desc}  (limit {limit:.0f}s)")
            except Exception as e:
                print(f"    skipped   {m.desc}: {type(e).__name__}")
            finally:
                path.write_text(original)
        n = killed + len(survived)
        timeouts = f" ({by_timeout} by timeout)" if by_timeout else ""
        print(f"    => killed {killed}/{n}{timeouts}, SURVIVED {len(survived)}/{n}")

    # What the run did not measure, last, where the tail of a long run is
    # read. Not on a single-module CLI run, which says what it measured.
    # Deliberately unchecked here against TARGETS or the filesystem:
    # tests/test_mutation_probe_targets.py does that, and tests patch
    # TARGETS and REPO under main() with a ledger that matches neither.
    if len(rest) < 2:
        print(f"\n### NOT PROBED ({len(NOT_PROBED)} modules -- see NOT_PROBED "
              f"in scripts/mutation_probe.py)")
        for mod, why in NOT_PROBED.items():
            print(f"    {mod} -- {why}")

if __name__ == "__main__":
    main()
