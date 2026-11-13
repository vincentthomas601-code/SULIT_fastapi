import os
import logging
import numpy as np
import onnxruntime as ort
from dsp_features import APPLIANCE_CLASSES

logger = logging.getLogger("MLEngine")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "whited_1d_resnet_150ep.onnx")
CONFIDENCE_THRESHOLD = 0.55


def softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits)
    exp_logits = np.exp(shifted)
    return exp_logits / np.sum(exp_logits)


class NILMClassifier:
    def __init__(self, model_path: str = MODEL_PATH):
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"ONNX model not found at '{model_path}'")

        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        input_meta = self.session.get_inputs()[0]
        self.input_name = input_meta.name
        self.input_shape = list(input_meta.shape)

        # Dynamically read expected feature dimension (40 for input_fused_features_40d)
        last_dim = self.input_shape[-1] if self.input_shape else 40
        self.expected_features = int(last_dim) if isinstance(last_dim, int) and last_dim > 0 else 40

        logger.info(
            f"Loaded ONNX model '{model_path}' (input='{self.input_name}', shape={self.input_shape})."
        )

    def predict(self, features: list | np.ndarray) -> tuple[str, float]:
        """
        Runs ONNX inference using the exact tensor rank (2D or 3D) expected by the model.
        Returns: (appliance_name: str, confidence: float)
        """
        feature_arr = np.asarray(features, dtype=np.float32).flatten()
        if feature_arr.size != self.expected_features:
            raise ValueError(f"Expected {self.expected_features} features, got {feature_arr.size}")

        if len(self.input_shape) == 3:
            input_tensor = feature_arr.reshape(1, 1, self.expected_features)
        else:
            input_tensor = feature_arr.reshape(1, self.expected_features)

        outputs = self.session.run(None, {self.input_name: input_tensor})
        logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)

        probs = softmax(logits)
        predicted_idx = int(np.argmax(probs))
        confidence = round(float(probs[predicted_idx]), 4)

        if confidence < CONFIDENCE_THRESHOLD or predicted_idx >= len(APPLIANCE_CLASSES):
            return "Unknown Load", confidence

        return APPLIANCE_CLASSES[predicted_idx], confidence


classifier = NILMClassifier()