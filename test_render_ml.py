import requests
import numpy as np

RENDER_URL = "https://sulit-fastapi.onrender.com/api/v1/events/transient"

sample_rate = 30000
duration = 0.2
t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
signal = 2.5 * np.sin(2 * np.pi * 60 * t) + np.random.normal(0, 0.05, len(t))

payload = {
    "device_id": "SIMULATED_ESP32_01",
    "p_before": 850.0,
    "p_after": 50.0,   # Delta P = -800.0 W (TURN_OFF step)
    "pf_before": 0.97,
    "pf_after": 0.85,
    "raw_samples": signal.tolist()
}

print("Pinging Render server with TURN_OFF transient payload...")
response = requests.post(RENDER_URL, json=payload, timeout=20)
print(f"Status Code: {response.status_code}")
print("Response Payload:", response.json())