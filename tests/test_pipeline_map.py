"""Guards that the /signal-processing lineage stays in sync with the real stage code."""
import pipeline_map


def test_downstream_contract_matches_s3_predictor_vector():
    """The dashboard's S3 contract must equal S3's ACTUAL model input vector — else the
    review page lies about what the model consumes."""
    import s3_predictors
    assert (pipeline_map.DOWNSTREAM_INPUTS["S3 model (PREDICTOR_FEATURES)"]
            == list(s3_predictors.PREDICTOR_FEATURES))


def test_every_predictor_feature_is_produced_by_an_s2_group():
    """Every model input must be traceable to an S2 lineage group (no orphan input the
    dashboard can't explain)."""
    import s3_predictors
    produced = {f for _, feats, _, _ in pipeline_map.LINEAGE for f in feats}
    for f in s3_predictors.PREDICTOR_FEATURES:
        assert f in produced, f"{f} consumed by S3 but not in any S2 lineage group"


def test_step_classification_is_correct():
    """The single-stock page must file each signal under the right pipeline step —
    note fundamental.* is S1 but fund.* is S2 (distinct prefixes)."""
    f = pipeline_map._step_of
    assert f("price.close") == "S1 · Raw"
    assert f("fundamental.statements") == "S1 · Raw"      # S1, not fund.*
    assert f("short.pct_float") == "S1 · Raw"
    assert f("tech.intraday_ret") == "S2 · Signals"
    assert f("fund.market_cap") == "S2 · Signals"          # fund.* is S2
    assert f("earnings.analysis") == "S2 · Signals"        # derived, not raw
    assert f("earnings.report_raw") == "S1 · Raw"
    assert f("predict.eod_return") == "S3 · Predictors"
    assert f("alpha.signal") == "S4 · Alpha"


def test_gaps_reference_real_s1_tables():
    from schema import SCHEMA
    for g in pipeline_map.GAPS:
        # first table token (some gaps name a representative table)
        assert g["s1_table"] in SCHEMA, f"gap {g['signal']} names unknown table {g['s1_table']}"
        assert g["severity"] in ("missing", "unused")
