# Latency Optimization Experiments

## Experiment 01: HTTP Client Reuse
- AS-IS: Create and close AsyncClient for every request
- TO-BE: Reuse AsyncClient throughout server lifecycle
- Baseline: 205.07 ms (n=3)
- After optimization: 16.88 ms (n=12)
- Observed reduction: 91.8%

## Experiment 02: Frame Stability Optimization

| Parameter | Original | Updated |
|---|---:|---:|
| STABLE_SECONDS | 0.25 | 0.15 |
| MIN_SAMPLES | 3 | 2 |
| MAX_MEAN_CHANGE | 7.0 | 12.0 |
| MAX_CHANGED_FRACTION | 0.08 | 0.15 |

### MAX_CHANGED_FRACTION: 0.08 vs 0.15
- Mean: 909.4 ms -> 521.0 ms
- Median: 699 ms -> 227 ms
- Mean reduction: 42.7%
- Within 300 ms: 42.9% -> 60.0%
- Within 1 second: 71.4% -> 90.0%
- Samples: 7 before / 10 after

## Limitations
- Small sample sizes
- Uncontrolled camera movement and scene conditions
- Recognition accuracy has not been quantitatively evaluated
- WebRTC transmission latency has not been measured

## Next Steps
- Evaluate recognition quality
- Separate scene analysis from audio playback
- Optimize backend inference and speech generation