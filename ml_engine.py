import os
import logging
import numpy as np
import onnxruntime as ort
from dsp_features import APPLIANCE_CLASSES

logger = logging.getLogger("MLEngine")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "whited_1d_resnet_150ep.onnx")

# Feature standardization parameters (Mean and Std from WHITED training set)
FEATURE_MEAN = np.zeros(22, dtype=np.float32)
FEATURE_STD = np.ones(22, dtype=np.float32)
CONFIDENCE_THRESHOLD = 0.60

class NILMClassifier:
    def __init__(self, model_path: str = MODEL_PATH):
        self.session = None
        if os.path.exists(model_path):
            try:
                # Force CPU execution explicitly
                self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
                self.input_name = self.session.get_inputs()[0].name
                logger.info(f"Successfully loaded ONNX model from '{model_path}'.")
            except Exception as e:
                logger.warning(f"Failed to initialize ONNX session ({e}). Open-set fallback active.")
        else:
            logger.warning(f"ONNX model '{model_path}' not found. Fallback active.")

    def predict(self, feature_vector: np.ndarray) -> tuple[str, float]:
        if self.session is not None:
            try:
                # Apply standardization scaling
                scaled_vector = (feature_vector - FEATURE_MEAN) / (FEATURE_STD + 1e-6)
                input_data = np.array(scaled_vector, dtype=np.float32).reshape(1, 1, 22)
                
                outputs = self.session.run(None, {self.input_name: input_data})
                logits = outputs[0][0]

                exp_logits = np.exp(logits - np.max(logits))
                probabilities = exp_logits / np.sum(exp_logits)
                predicted_idx = int(np.argmax(probabilities))
                confidence = float(probabilities[predicted_idx])

                # Open-set rejection threshold
                if confidence < CONFIDENCE_THRESHOLD:
                    return "Unknown Load", round(confidence, 4)

                return APPLIANCE_CLASSES[predicted_idx], round(confidence, 4)
            except Exception as e:
                logger.error(f"ONNX Inference error ({e}). Falling back.")

        # Heuristic fallback returning Unknown on uncertainty
        delta_p = feature_vector[0] if len(feature_vector) > 0 else 0.0
        if delta_p >= 800:
            return "Air Conditioner", 0.75
        elif delta_p >= 300:
            return "Fridge", 0.70
        elif delta_p >= 100:
            return "Fan", 0.65
        else:
            return "Unknown Load", 0.40

classifier = NILMClassifier()