import pytest


@pytest.fixture(autouse=True)
def _no_metric_depth_download(monkeypatch):
    """Step tests must not download the metric depth model; the cue is tested on its own."""
    from uncertainscene.change import metric

    def offline(model_id):
        raise RuntimeError("metric depth model not available in tests")

    monkeypatch.setattr(metric, "_predictor", offline)
