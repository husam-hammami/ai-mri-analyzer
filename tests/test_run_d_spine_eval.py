from pathlib import Path

from validation import spine_eval


def test_stage_info_includes_spider_and_rsna_scope():
    info = spine_eval.stage_info()

    assert "zenodo.org/records/8009680" in info["spider_record"]
    assert info["files"]["images.zip"]["bytes"] > 1_000_000_000
    assert info["rsna_lumbardisc"]["status"] == "kaggle_or_rsna_gated"


def test_arm_tagged_cache_keeps_baseline_legacy_path(tmp_path):
    base = spine_eval._cache_dir(tmp_path, "Case/001", "baseline")
    arm = spine_eval._cache_dir(tmp_path, "Case/001", "hunt")

    assert base.name == "case001"
    assert arm.name == "case001__hunt"


def test_spider_label_parser_aggregates_per_case(tmp_path):
    (tmp_path / "images").mkdir()
    image = tmp_path / "images" / "caseA.mha"
    image.write_text("placeholder", encoding="utf-8")
    (tmp_path / "radiological_gradings.csv").write_text(
        "study_id,level,pfirrmann,disc_herniation,disc_bulging,modic\n"
        "caseA,L4-L5,4,1,0,none\n"
        "caseA,L5-S1,2,0,0,1\n",
        encoding="utf-8",
    )

    labels = spine_eval.load_spider_labels(tmp_path)
    cases = spine_eval.discover_spider_cases(tmp_path)

    assert labels["casea"]["disc_herniation"] is True
    assert labels["casea"]["disc_bulging"] is False
    assert labels["casea"]["pfirrmann_advanced"] is True
    assert labels["casea"]["modic_change"] is True
    assert cases[0].case_id == "caseA"
    assert cases[0].labels["disc_herniation"] is True


def test_discover_groups_sequences_by_subject(tmp_path):
    img = tmp_path / "images"
    img.mkdir()
    for name in ("100_t1.mha", "100_t2.mha", "101_t2.mha"):
        (img / name).write_text("x", encoding="utf-8")
    cases = spine_eval.discover_spider_cases(tmp_path)
    by_id = {c.case_id: c for c in cases}
    assert set(by_id) == {"100", "101"}            # grouped by subject, not per file
    assert len(by_id["100"].image_paths) == 2      # T1 + T2 read as ONE study
    assert len(by_id["101"].image_paths) == 1


def test_metrics_persist_raw_counts_and_kn():
    counts = {finding: {"tp": 0, "fp": 0, "tn": 0, "fn": 0} for finding in spine_eval.FINDINGS}
    spine_eval.update_counts(
        counts,
        {"disc_herniation": True, "disc_bulging": False, "modic_change": None},
        {"disc_herniation": 1, "disc_bulging": 1, "modic_change": 1},
    )

    metrics = spine_eval.metrics_from_counts(counts)

    assert metrics["disc_herniation"]["tp"] == 1
    assert metrics["disc_herniation"]["sensitivity_kN"] == "1/1"
    assert metrics["disc_bulging"]["fp"] == 1
    assert metrics["disc_bulging"]["specificity_kN"] == "0/1"
    assert metrics["modic_change"]["sensitivity_kN"] == "n/a"


def test_read_confirmation_rejects_empty_or_instant_zero_cost():
    assert not spine_eval.read_confirmed({}, cached=False, elapsed_s=30, cost_usd=1.0, success=True)
    assert not spine_eval.read_confirmed(
        {"impression": ["Normal lumbar spine."]},
        cached=False,
        elapsed_s=0.2,
        cost_usd=0.0,
        success=True,
    )
    assert spine_eval.read_confirmed(
        {"impression": ["Normal lumbar spine."]},
        cached=True,
        elapsed_s=0.0,
        cost_usd=0.0,
        success=True,
    )


def test_dataset_guard_rejects_repo_paths():
    try:
        spine_eval.require_non_onedrive(Path(__file__).resolve().parents[1])
    except ValueError as exc:
        assert "outside the repo" in str(exc)
    else:
        raise AssertionError("repo path should be rejected as a dataset root")
