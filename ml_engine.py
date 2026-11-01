import os
import numpy as np
import onnxruntime as ort
from dsp_features import APPLIANCE_CLASSES

MODEL_PATH = os.path.join(os.path.dirname(__file__), "sulit_model.onnx")

class NILMClassifier:
    def __init__(self, model_path: str = MODEL_PATH):
        self.session = None
        if os.path.exists(model_path):
            self.session = ort.InferenceSession(model_path)
            self.input_name = self.session.get_inputs()[0].name
            print(f"[NILM Engine] Successfully loaded ONNX model from '{model_path}'.")
        else:
            print(f"[NILM Engine] Notice: '{model_path}' not found. Rule-based AP fallback active.")

    def predict(self, feature_vector: np.ndarray) -> tuple[str, float]:
        """Runs ONNX model inference and returns (appliance_label, confidence_score)."""
        if self.session is None:
            # Rule-based AP fallback classifier if model file is not yet uploaded
            delta_p = feature_vector[0]
            if delta_p >= 800:
                return "Air Conditioner", 0.92
            elif delta_p >= 300:
                return "Fridge", 0.88
            elif delta_p >= 100:
                return "Fan", 0.80
            else:
                return "Lighting", 0.75

        # Format input array for ONNX tensor [batch_size=1, features=22]
        input_data = np.expand_dims(feature_vector, axis=0).astype(np.float32)
        outputs = self.session.run(None, {self.input_name: input_data})
        logits = outputs[0][0]

        # Compute Softmax probabilities
        exp_logits = np.exp(logits - np.max(logits))
        probabilities = exp_logits / np.sum(exp_logits)
        predicted_idx = int(np.argmax(probabilities))
        confidence = float(probabilities[predicted_idx])

        return APPLIANCE_CLASSES[predicted_idx], round(confidence, 4)

classifier = NILMClassifier()