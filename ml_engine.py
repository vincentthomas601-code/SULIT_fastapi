import os
import logging
import numpy as np
import onnxruntime as ort
from dsp_features import APPLIANCE_CLASSES

logger = logging.getLogger("MLEngine")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "whited_1d_resnet_150ep.onnx")
CONFIDENCE_THRESHOLD = 0.55


def softmax(logits: np.ndarray) -> np.ndarray:
    exp_logits = np.exp(logits - np.max(logits))
    return exp_logits / np.sum(exp_logits)


class NILMClassifier:
    def __init__(self, model_path: str = MODEL_PATH):
        self.session = None
        self.expected_features = 22

        if not os.path.exists(model_path):
            raise FileNotFoundError(f"ONNX model not found at '{model_path}'")

        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        logger.info(f"Loaded ONNX model '{model_path}' successfully.")

    def predict(self, features: list | np.ndarray) -> tuple[str, float]:
        """
        Runs pure ONNX inference on the 22D DSP feature vector.
        Returns: (appliance_name: str, confidence: float)
        """
        feature_arr = np.array(features, dtype=np.float32).flatten()
        if feature_arr.size != self.expected_features:
            raise ValueError(f"Expected {self.expected_features} features, got {feature_arr.size}")

        input_tensor = feature_arr.reshape(1, 1, self.expected_features)
        outputs = self.session.run(None, {self.input_name: input_tensor})
        logits = outputs[0][0]

        probs = softmax(logits)
        predicted_idx = int(np.argmax(probs))
        confidence = round(float(probs[predicted_idx]), 4)

        if confidence < CONFIDENCE_THRESHOLD or predicted_idx >= len(APPLIANCE_CLASSES):
            return "Unknown Load", confidence

        return APPLIANCE_CLASSES[predicted_idx], confidence


classifier = NILMClassifier()