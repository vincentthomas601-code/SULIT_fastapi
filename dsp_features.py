import numpy as np

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
    Extracts exact 22-dimensional feature vector by calculating the DIFFERENCE 
    between the pre-transient window and post-transient window.
    """
    signal = np.array(raw_samples, dtype=np.float32)
    
    # Split into pre-event and post-event windows (3000 samples each)
    midpoint = len(signal) // 2
    pre_window = signal[:midpoint]
    post_window = signal[midpoint:]

    # 1. Remove ADC DC Offset per window independently
    pre_window = pre_window - np.mean(pre_window)
    post_window = post_window - np.mean(post_window)

    # Features 1-2: Steady-state step changes (PZEM)
    delta_p = float(p_after - p_before)
    delta_pf = float(pf_after - pf_before)

    def extract_window_metrics(win: np.ndarray) -> np.ndarray:
        rms_val = np.sqrt(np.mean(win ** 2))
        peak_val = np.max(np.abs(win))
        crest_factor = peak_val / (rms_val + 1e-6)
        std_val = np.std(win)
        
        diff_win = np.diff(win)
        max_slope = np.max(np.abs(diff_win)) if len(diff_win) > 0 else 0.0
        mean_slope = np.mean(np.abs(diff_win)) if len(diff_win) > 0 else 0.0
        
        zc_count = len(np.where(np.diff(np.signbit(win)))[0])

        fft_spectrum = np.abs(np.fft.rfft(win))
        # 60Hz Fundamental Bin fixed for 3000 samples @ 30kHz (10 Hz per bin -> Bin 6)
        fundamental_idx = 6 if len(fft_spectrum) > 6 else np.argmax(fft_spectrum[1:]) + 1
        i1 = fft_spectrum[fundamental_idx] + 1e-6

        harmonics = []
        for h in range(2, 13):
            h_idx = min(fundamental_idx * h, len(fft_spectrum) - 1)
            harmonics.append(fft_spectrum[h_idx] / i1)

        thd = np.sqrt(np.sum(np.array(harmonics) ** 2))

        return np.array([
            rms_val, peak_val, crest_factor, std_val,
            max_slope, mean_slope, zc_count, thd, i1, *harmonics
        ], dtype=np.float32)

    # Compute differential features (Post - Pre)
    pre_metrics = extract_window_metrics(pre_window)
    post_metrics = extract_window_metrics(post_window)
    diff_metrics = post_metrics - pre_metrics

    # Assemble exact 22D vector
    features = np.concatenate(([delta_p, delta_pf], diff_metrics))
    
    if len(features) < 22:
        features = np.pad(features, (0, 22 - len(features)), 'constant')
    elif len(features) > 22:
        features = features[:22]

    return features