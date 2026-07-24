"""models/wrapper.py — the loadable Predictor API for promoted models.

The pipeline (S3) never trains inline — it LOADS a promoted model through this
wrapper. Decouples the live pipeline from any specific model: swap the promoted
model, the pipeline code doesn't change. A model that isn't promoted+registered
here cannot reach the pipeline.

  Predictor.load(model_id) -> Predictor
  .predict(features: dict|list) -> float
  .metadata -> the committed metadata.json
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent


def list_models() -> dict:
    reg = MODELS_DIR / "registry.json"
    return json.loads(reg.read_text())["models"] if reg.exists() else {}


class Predictor:
    def __init__(self, trained: dict, metadata: dict):
        self._t = trained
        self.metadata = metadata
        self.feature_names = trained["feature_names"]

    @classmethod
    def load(cls, model_id: str) -> "Predictor":
        mdir = MODELS_DIR / model_id
        meta = json.loads((mdir / "metadata.json").read_text())
        artifact = mdir / "artifact.pkl"
        if not artifact.exists():
            raise FileNotFoundError(
                f"{model_id} metadata is registered but its artifact.pkl is "
                f"missing (gitignored — rebuild via {meta.get('experiment')}).")
        with open(artifact, "rb") as f:
            trained = pickle.load(f)
        return cls(trained, meta)

    def predict(self, features) -> float:
        """features: dict {feature_name: value} or a list aligned to
        feature_names. Missing values -> mean-imputed (0 after standardization)."""
        import numpy as np
        if isinstance(features, dict):
            row = [features.get(f, np.nan) for f in self.feature_names]
        else:
            row = list(features)
        X = np.array([row], dtype=float)
        Xz = np.where(np.isnan(X), 0.0, (X - self._t["mean"]) / self._t["std"])
        return float(self._t["model"].predict(Xz)[0])
