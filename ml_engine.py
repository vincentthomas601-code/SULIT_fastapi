import os
import logging
import numpy as np
import onnxruntime as ort
from dsp_features import APPLIANCE_CLASSES

logger = logging.getLogger("MLEngine")

BASE_DIR = os.path.dirname(__file__)
CONFIDENCE_THRESHOLD = 0.55

MODEL_FILES = {
    "Fridge": "fridge_tcn_wavenet.onnx",
    "Kettle": "kettle_tcn_wavenet.onnx",
    "Microwave": "microwave_tcn_wavenet.onnx",
    "Rice Cooker": "rice_cooker_tcn_wavenet.onnx",
    "TV": "tv_tcn_wavenet.onnx",
    "Washing Machine": "washing_machine_tcn_wavenet.onnx",
}


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits)
    exp_logits = np.exp(shifted)
    return exp_logits / (np.sum(exp_logits) + 1e-9)


def sigmoid(x: float) -> float:
    clipped = float(np.clip(x, -50.0, 50.0))
    return float(1.0 / (1.0 + np.exp(-clipped)))


class MultiModelNILMClassifier:
    def __init__(self, model_files: dict[str, str] = MODEL_FILES):
        self.models: dict[str, dict] = {}
        self.expected_features = 40

        for appliance_name, filename in model_files.items():
            model_path = os.path.join(BASE_DIR, filename)
            if not os.path.exists(model_path):
                logger.warning(f"ONNX model missing for '{appliance_name}' at '{model_path}'")
                continue

            session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            inputs_meta = [
                {"name": inp.name, "shape": list(inp.shape)}
                for inp in session.get_inputs()
            ]
            outputs_meta = [
                {"name": out.name, "shape": list(out.shape)}
                for out in session.get_outputs()
            ]

            self.models[appliance_name] = {
                "filename": filename,
                "session": session,
                "inputs": inputs_meta,
                "outputs": outputs_meta,
            }
            logger.info(
                f"Loaded '{appliance_name}' ({filename}) -> inputs={inputs_meta}, outputs={outputs_meta}"
            )

        if not self.models:
            raise FileNotFoundError(
                f"No TCN-WaveNet ONNX models found in '{BASE_DIR}'. Expected: {list(model_files.values())}"
            )

    def _prepare_waveform(self, raw_samples: list[float] | np.ndarray | None, target_len: int) -> np.ndarray:
        if raw_samples is None or len(raw_samples) == 0:
            return np.zeros(target_len, dtype=np.float32)

        sig = np.nan_to_num(np.asarray(raw_samples, dtype=np.float32).flatten(), nan=0.0, posinf=0.0, neginf=0.0)
        sig = sig - np.mean(sig)
        std = float(np.std(sig))
        if std > 1e-6:
            sig = sig / std

        if sig.size == target_len:
            return sig.astype(np.float32)

        old_idx = np.linspace(0.0, 1.0, num=sig.size, dtype=np.float32)
        new_idx = np.linspace(0.0, 1.0, num=target_len, dtype=np.float32)
        return np.interp(new_idx, old_idx, sig).astype(np.float32)

    def _build_input_feed(
        self,
        inputs_meta: list[dict],
        raw_samples: list[float] | np.ndarray | None,
        features_40d: np.ndarray,
    ) -> dict[str, np.ndarray]:
        feed: dict[str, np.ndarray] = {}
        feat_arr = np.asarray(features_40d, dtype=np.float32).flatten()
        raw_len = len(raw_samples) if raw_samples is not None and len(raw_samples) > 0 else 6000

        for inp in inputs_meta:
            name = inp["name"]
            shape = inp["shape"]

            if len(shape) == 3:
                d1, d2 = shape[1], shape[2]
                # Channels-first: (batch, 1, seq_len) — standard PyTorch Conv1d / TCN-WaveNet
                if d1 == 1:
                    seq_len = int(d2) if isinstance(d2, int) and d2 > 0 else raw_len
                    if seq_len == feat_arr.size:
                        arr = feat_arr
                    else:
                        arr = self._prepare_waveform(raw_samples, seq_len)
                    feed[name] = arr.reshape(1, 1, seq_len)
                # Channels-last: (batch, seq_len, 1) — standard Keras Conv1D / TCN-WaveNet
                elif d2 == 1:
                    seq_len = int(d1) if isinstance(d1, int) and d1 > 0 else raw_len
                    if seq_len == feat_arr.size:
                        arr = feat_arr
                    else:
                        arr = self._prepare_waveform(raw_samples, seq_len)
                    feed[name] = arr.reshape(1, seq_len, 1)
                else:
                    c = int(d1) if isinstance(d1, int) and d1 > 0 else 1
                    seq_len = int(d2) if isinstance(d2, int) and d2 > 0 else raw_len
                    arr = self._prepare_waveform(raw_samples, seq_len)
                    feed[name] = np.tile(arr, (1, c, 1)).astype(np.float32)

            elif len(shape) == 2:
                dim = shape[1]
                feat_len = int(dim) if isinstance(dim, int) and dim > 0 else feat_arr.size
                if feat_len <= feat_arr.size:
                    arr = feat_arr[:feat_len]
                else:
                    arr = self._prepare_waveform(raw_samples, feat_len)
                feed[name] = arr.reshape(1, feat_len)

            else:
                dim = shape[0] if shape else feat_arr.size
                feat_len = int(dim) if isinstance(dim, int) and dim > 0 else feat_arr.size
                arr = feat_arr[:feat_len] if feat_len <= feat_arr.size else self._prepare_waveform(raw_samples, feat_len)
                feed[name] = arr.reshape(feat_len)

        return feed

    def _score_output(self, outputs: list[np.ndarray], delta_p: float) -> float:
        out = np.asarray(outputs[0], dtype=np.float32)

        # Binary 2-logit or 2-probability output: [OFF, ON]
        if out.ndim >= 2 and out.shape[-1] == 2:
            vec = out[0, out.shape[1] // 2] if out.ndim == 3 else out[0]
            if 0.0 <= float(vec[0]) <= 1.0 and 0.0 <= float(vec[1]) <= 1.0 and abs(float(np.sum(vec)) - 1.0) < 1e-2:
                return float(vec[1])
            return float(softmax(vec)[1])

        flat = out.reshape(-1)
        if flat.size == 0:
            return 0.0

        # Seq2Seq sequence -> take midpoint (Seq2Point); scalar -> single value
        val = float(flat[flat.size // 2]) if flat.size > 1 else float(flat[0])

        # Already probability / normalized activation [0, 1]
        if 0.0 <= val <= 1.0:
            return val

        # Negative logit -> probability < 0.5
        if val < 0.0:
            return sigmoid(val)

        # Seq2Point wattage regression output (> 15 W) -> compare predicted Watts vs measured delta_p
        if val > 15.0 and delta_p > 0.0:
            rel_err = abs(val - delta_p) / max(delta_p, 150.0)
            return float(max(0.0, 1.0 - rel_err))

        # Positive logit (1.0 < val <= 15.0)
        return sigmoid(val)

    def predict(
        self,
        features: list | np.ndarray,
        raw_samples: list[float] | np.ndarray | None = None,
        delta_p: float = 0.0,
    ) -> tuple[str, float]:
        """
        Runs inference across all loaded per-appliance TCN-WaveNet ONNX models.
        Returns: (appliance_name: str, confidence: float)
        """
        features_40d = np.asarray(features, dtype=np.float32).flatten()
        scores: dict[str, float] = {}

        for appliance_name, entry in self.models.items():
            feed = self._build_input_feed(entry["inputs"], raw_samples, features_40d)
            outputs = entry["session"].run(None, feed)
            scores[appliance_name] = self._score_output(outputs, delta_p)

        best_appliance = max(scores, key=scores.get)
        best_confidence = round(float(scores[best_appliance]), 4)

        logger.info(f"TCN-WaveNet scores (dP={delta_p:.1f}W): { {k: round(v, 4) for k, v in scores.items()} }")

        if best_confidence < CONFIDENCE_THRESHOLD or best_appliance not in APPLIANCE_CLASSES:
            return "Unknown Load", best_confidence

        return best_appliance, best_confidence

    def get_status(self) -> dict:
        return {
            "loaded_count": len(self.models),
            "classes": list(self.models.keys()),
            "models": {
                name: {
                    "filename": info["filename"],
                    "inputs": info["inputs"],
                    "outputs": info["outputs"],
                }
                for name, info in self.models.items()
            },
        }


classifier = MultiModelNILMClassifier()