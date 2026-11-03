| Configuration | Rate (tx/s) | Decided in window (tx/s) | p50 (ms) | p99 (ms) | max (ms) | Lost | CPU of the busiest scorer |
|---|---|---|---|---|---|---|---|
| heuristic-memory-1 | 12000 | 11987 | 6 | 14 | 59 | 0 | 59% |
| heuristic-memory-1 | 16000 | 15974 | 6 | 13 | 20 | 0 | 85% |
| heuristic-memory-1 | 24000 | 16705 | 1059 | 3148 | 3184 | 0 | 92% |
| lightgbm-memory-1 | 12000 | 11983 | 10 | 50 | 77 | 0 | 92% |
| lightgbm-memory-1 | 16000 | 14243 | 370 | 734 | 742 | 0 | 80% |
| lightgbm-memory-1 | 24000 | 14300 | 2012 | 4100 | 4147 | 0 | 100% |
| lightgbm-redis-1 | 12000 | 11977 | 9 | 31 | 41 | 0 | 69% |
| lightgbm-redis-1 | 16000 | 15955 | 12 | 56 | 68 | 0 | 76% |
| lightgbm-redis-1 | 24000 | 21031 | 292 | 843 | 862 | 0 | 71% |
| lightgbm-redis-3 | 12000 | 11987 | 8 | 14 | 22 | 0 | 30% |
| lightgbm-redis-3 | 16000 | 15970 | 8 | 24 | 60 | 0 | 49% |
| lightgbm-redis-3 | 24000 | 23956 | 9 | 32 | 47 | 0 | 62% |
