### evasion from hour 450

| Window (hours) | Fraud | Flag rate | Worst PSI (used features) | AP | Recall at review |
|---|---|---|---|---|---|
| 356-379 | 260 | 0.123% | 0.03 (hour) | 0.9999 | 1.000 |
| 380-403 | 298 | 0.109% | 0.02 (amount_log) | 0.9985 | 1.000 |
| 404-427 | 308 | 0.331% | 3.56 (hour) | 1.0000 | 1.000 |
| 428-451 | 232 | 1.290% | 1.90 (hour) | 0.9482 | 0.935 |
| 452-475 | 254 | 0.285% | 2.55 (hour) | 0.2714 | 0.146 |
| 476-499 | 242 | 0.271% | 1.81 (hour) | 0.2353 | 0.157 |
| 500-523 | 282 | 0.287% | 2.60 (hour) | 0.2597 | 0.206 |
| 524-547 | 204 | 0.070% | 0.65 (hour) | 0.1300 | 0.098 |
| 548-571 | 278 | 0.127% | 0.63 (hour) | 0.2199 | 0.155 |
| 572-595 | 250 | 0.086% | 0.63 (hour) | 0.1687 | 0.116 |
| 596-619 | 268 | 0.187% | 2.02 (hour) | 0.2552 | 0.168 |
| 620-643 | 262 | 0.635% | 2.53 (hour) | 0.3936 | 0.176 |
| 644-667 | 258 | 0.419% | 1.99 (hour) | 0.2881 | 0.178 |
| 668-691 | 250 | 0.241% | 1.63 (hour) | 0.3183 | 0.272 |
| 692-715 | 272 | 0.269% | 2.37 (hour) | 0.3117 | 0.202 |
| 716-739 | 282 | 2.634% | 2.99 (hour) | 0.6525 | 0.181 |
| 740-743 | 50 | 28.000% | 7.29 (hour) | 1.0000 | 0.280 |

Held-out hours 650-743 (1038 fraud): model in service AP 0.3028, recall 0.205 and false alarms 0.033% of honest payments at its review threshold (target 0.100%).

| Retrain at hour | Labels known up to | Drifted fraud in training / validation | Gate | Held-out AP | Held-out recall at its review threshold | False alarms |
|---|---|---|---|---|---|---|
| 640 | 640 | 1246 / 364 | pass | 0.9023 | 0.817 | 0.1291% |

### inflation from hour 450

| Window (hours) | Fraud | Flag rate | Worst PSI (used features) | AP | Recall at review |
|---|---|---|---|---|---|
| 356-379 | 260 | 0.123% | 0.03 (hour) | 0.9999 | 1.000 |
| 380-403 | 298 | 0.109% | 0.02 (amount_log) | 0.9985 | 1.000 |
| 404-427 | 308 | 0.331% | 3.56 (hour) | 1.0000 | 1.000 |
| 428-451 | 232 | 1.663% | 1.90 (hour) | 1.0000 | 1.000 |
| 452-475 | 254 | 8.067% | 2.55 (hour) | 1.0000 | 1.000 |
| 476-499 | 242 | 6.139% | 1.81 (hour) | 1.0000 | 1.000 |
| 500-523 | 282 | 5.251% | 2.60 (hour) | 0.9998 | 1.000 |
| 524-547 | 204 | 4.203% | 0.65 (hour) | 0.9931 | 1.000 |
| 548-571 | 278 | 5.378% | 0.64 (amount_log) | 0.9975 | 1.000 |
| 572-595 | 250 | 4.968% | 0.64 (amount_log) | 0.9973 | 1.000 |
| 596-619 | 268 | 2.937% | 2.02 (hour) | 1.0000 | 1.000 |
| 620-643 | 262 | 8.871% | 2.53 (hour) | 1.0000 | 1.000 |
| 644-667 | 258 | 7.280% | 1.99 (hour) | 0.9972 | 1.000 |
| 668-691 | 250 | 6.472% | 1.63 (hour) | 0.9994 | 1.000 |
| 692-715 | 272 | 5.753% | 2.37 (hour) | 0.9982 | 1.000 |
| 716-739 | 282 | 15.238% | 2.99 (hour) | 0.9999 | 1.000 |
| 740-743 | 50 | 100.000% | 7.29 (hour) | 1.0000 | 1.000 |

Held-out hours 650-743 (1038 fraud): model in service AP 0.9985, recall 1.000 and false alarms 5.487% of honest payments at its review threshold (target 0.100%).

| Retrain at hour | Labels known up to | Drifted fraud in training / validation | Gate | Held-out AP | Held-out recall at its review threshold | False alarms |
|---|---|---|---|---|---|---|
| 640 | 640 | 1246 / 364 | pass | 0.9993 | 1.000 | 0.1115% |
