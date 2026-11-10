# HUT & SEEK — Window Alert

**HUT & SEEK: Window Alert** analyzes 2021 NFL Next Gen Stats player tracking and PFF route assignments to show broadcasters, coaches, and scouts how long WRs, TEs, and RBs maintain separation before pass release.
Across **7519 eligible passing plays** and **34368 receiver-play observations** across **122 games** (2021 regular season, Weeks 1–8), **33.7%** of observed receivers created at least 3 yards of nearest-defender separation at some point between 1.0 s after the snap and pass release but were below that threshold at release; the featured replay of **Parris Campbell** (game 2021091206, play 2111) showed a separation window that closed before pass release.
The Python tool joins supplied play, player, PFF, and tracking CSVs, calculates 10-Hz Euclidean nearest-defender distances from snap through the `pass_forward` event (counting a window as open only from 1.0 s after the snap, so pre-snap defender cushion is excluded), and displays separation-window duration on an annotated field and timeline.
This is a descriptive sample of 122 games, the threshold is an exploratory proxy rather than a validated definition of being open, and tracking proximity alone cannot establish passing-lane visibility, quarterback reads, or causation.
Run `pip install -r requirements.txt` and then `python window_alert.py --data /path/to/nfl-big-data-bowl-regional-event-data/data` to regenerate the results.

![Window Alert tracking replay](output/window_alert.png)

![Window lifetime by position](output/window_by_position.png)

[Per-receiver metrics](output/window_metrics.csv) · [Per-game summary](output/game_summary.csv) · [Selected play tracking timeline](output/featured_window_timeline.csv)
