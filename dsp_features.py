import numpy as np
from scipy.fft import fft

# 11 appliance classes retained from the WHITED dataset
APPLIANCE_CLASSES = [
    "Air Conditioner", "Fan", "Fridge", "Hair Dryer", "Heater", 
    "Kettle", "Lighting", "Microwave", "Pump", "Toaster", "Vacuum"
]

def extract_22d_features(
    raw_samples: list[float], 
    p_before: float, 
    p_after: float, 
    pf_before: float, 
    pf_after: float
) -> np.ndarray:
    """
    Extracts the 22-dimensional feature vector:
    - Features 1-2: Path A PZEM power step (Delta P) and power factor step (Delta PF)
    - Features 3-22: Path B SCT-013 30 kHz current transient window metrics (6,000 samples)
    """
    signal = np.array(raw_samples, dtype=np.float32)

    # Features 1-2: Steady-state steps from PZEM (Path A)
    delta_p = p_after - p_before
    delta_pf = pf_after - pf_before

    # Features 3-10: Waveform Time-Domain Metrics (Path B)
    rms_val = float(np.sqrt(np.mean(signal ** 2)))
    peak_val = float(np.max(np.abs(signal)))
    crest_factor = float(peak_val / (rms_val + 1e-6))
    std_val = float(np.std(signal))
    max_slope = float(np.max(np.abs(np.diff(signal))))
    mean_slope = float(np.mean(np.abs(np.diff(signal))))

    # Zero-crossing dynamics (phase alignment)
    zero_crossings = np.where(np.diff(np.signbit(signal)))[0]
    zc_count = float(len(zero_crossings))

    # Features 11-22: Frequency Domain & Normalized Harmonics (I_h / I_1)
    n = len(signal)
    fft_spectrum = np.abs(fft(signal))[:n // 2]
    
    # Fundamental frequency bin (60 Hz grid)
    fundamental_idx = np.argmax(fft_spectrum[1:]) + 1
    i1 = fft_spectrum[fundamental_idx] + 1e-6

    # Extract harmonic amplitude ratios (h=2 through h=12 relative to fundamental)
    harmonics = []
    for h in range(2, 13):
        h_idx = min(fundamental_idx * h, len(fft_spectrum) - 1)
        harmonics.append(float(fft_spectrum[h_idx] / i1))

    thd = float(np.sqrt(np.sum(np.array(harmonics) ** 2)))

    # Combine into 22-dimensional array matching PyTorch model input order
    features = [
        delta_p, delta_pf, rms_val, peak_val, crest_factor, std_val,
        max_slope, mean_slope, zc_count, thd, *harmonics
    ]

    features_array = np.array(features, dtype=np.float32)
    
    # Enforce exact 22-dimension vector size
    if len(features_array) < 22:
        features_array = np.pad(features_array, (0, 22 - len(features_array)), 'constant')
    elif len(features_array) > 22:
        features_array = features_array[:22]

    return features_array