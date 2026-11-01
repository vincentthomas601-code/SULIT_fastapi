import os
import numpy as np
import onnxruntime as ort
from dsp_features import APPLIANCE_CLASSES

MODEL_PATH = os.path.join(os.path.dirname(__file__), "whited_1d_resnet_150ep.onnx")


class NILMClassifier:
    def __init__(self, model_path: str = MODEL_PATH):
        self.session = None
        if os.path.exists(model_path):
            self.session = ort.InferenceSession(model_path)
            self.input_name = self.session.get_inputs()[0].name
            print(f"[NILM Engine] Successfully loaded ONNX model from '{model_path}'.")
        else:
            print(f"[NILM Engine] Notice: '{model_path}' not found. Rule-based fallback active.")

    def predict(self, feature_vector: np.ndarray) -> tuple[str, float]:
        if self.session is None:
            delta_p = feature_vector[0]
            if delta_p >= 800:
                return "Air Conditioner", 0.92
            elif delta_p >= 300:
                return "Fridge", 0.88
            elif delta_p >= 100:
                return "Fan", 0.80
            else:
                return "Lighting", 0.75

        # Reshape 22D feature vector to 3D tensor (1, 1, 22) required for 1D ResNet Conv1d
        input_data = np.array(feature_vector, dtype=np.float32).reshape(1, 1, -1)
        
        outputs = self.session.run(None, {self.input_name: input_data})
        logits = outputs[0][0]

        exp_logits = np.exp(logits - np.max(logits))
        probabilities = exp_logits / np.sum(exp_logits)
        predicted_idx = int(np.argmax(probabilities))
        confidence = float(probabilities[predicted_idx])

        return APPLIANCE_CLASSES[predicted_idx], round(confidence, 4)


classifier = NILMClassifier()