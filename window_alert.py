"""HUT & SEEK — Window Alert | NFL NYU regional Big Data Bowl.

Uses only supplied NFL 2021 tracking CSVs (every game file by default) to quantify receiver separation
before pass release. Output: broadcast-style graphic, CSVs, 5-sentence README.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle

FRAME_SECONDS = 0.1  # 10 Hz in supplied tracking data
SETTLE_SECONDS = 1.0  # ignore first 1.0 s after snap so pre-snap defender cushion is not counted as an open window
SETTLE_FRAMES = int(round(SETTLE_SECONDS/FRAME_SECONDS))
FIELD_WIDTH = 53.3
GOLD = '#FCE45A'
GREEN = '#103E30'
DARK = '#081C19'
CREAM = '#F8F3E7'
ORANGE = '#FF8856'
PURPLE = '#B89BFB'
PALE = '#8BE0C0'
SAMPLE_LABEL = 'one-game sample'  # updated in main() to reflect how many games were analyzed


def find_data_dir(value: str) -> Path:
    base = Path(value).expanduser().resolve()
    options = [base, base/'data', base.parent/'data', Path.cwd()/'data']
    for d in options:
        if all((d/f).is_file() for f in ('plays.csv', 'players.csv', 'pffScoutingData.csv')) and (d/'tracking').is_dir():
            return d
    raise FileNotFoundError(
        'Could not find provided NFL data. Pass --data /path/to/nfl-big-data-bowl-regional-event-data/data. '
        'That folder must have plays.csv, players.csv, pffScoutingData.csv, and tracking/.')


def load_tables(data: Path):
    plays = pd.read_csv(data/'plays.csv', low_memory=False)
    players = pd.read_csv(data/'players.csv', low_memory=False)
    scouting = pd.read_csv(data/'pffScoutingData.csv', low_memory=False)
    for title, frame, cols in (
        ('plays', plays, {'gameId','playId','possessionTeam','defensiveTeam','passResult'}),
        ('players', players, {'nflId','officialPosition','displayName'}),
        ('pffScoutingData', scouting, {'gameId','playId','nflId','pff_role'}),
    ):
        missing = cols-set(frame.columns)
        if missing:
            raise ValueError(f'{title} missing columns: {sorted(missing)}')
    for frame in (plays, scouting):
        frame['gameId'] = pd.to_numeric(frame['gameId'], errors='coerce')
        frame['playId'] = pd.to_numeric(frame['playId'], errors='coerce')
    players['nflId'] = pd.to_numeric(players['nflId'], errors='coerce')
    scouting['nflId'] = pd.to_numeric(scouting['nflId'], errors='coerce')
    # The supplied README says pff_role = Pass route and officialPosition = WR/TE/RB.
    routes = scouting[scouting['pff_role'].fillna('').astype(str).str.strip().str.casefold() == 'pass route']
    routes = routes.merge(players[['nflId','officialPosition','displayName']], on='nflId', how='inner')
    routes = routes[routes['officialPosition'].fillna('').str.upper().isin(['WR','TE','RB'])]
    return plays, routes


def load_tracking(path: Path) -> pd.DataFrame:
    print(f'Reading NFL-provided tracking file: {path.name}', flush=True)
    t = pd.read_csv(path, low_memory=False)
    necessary = {'gameId','playId','nflId','frameId','team','x','y','event','playDirection'}
    missing = necessary-set(t.columns)
    if missing:
        raise ValueError(f'{path.name} missing columns: {sorted(missing)}')
    for col in ('gameId','playId','nflId','frameId','x','y'):
        t[col] = pd.to_numeric(t[col], errors='coerce')
    return t


def run_lengths(frames: np.ndarray) -> list[int]:
    """Length in sampled frames of contiguous spans; no interpolation across gaps."""
    if not len(frames):
        return []
    cuts = np.flatnonzero(np.diff(frames) != 1) + 1
    return [len(part) for part in np.split(frames, cuts)]


def calculate_window(receiver_rows: pd.DataFrame, defenders: pd.DataFrame,
                     snap: int, release: int, threshold: float):
    r = receiver_rows[(receiver_rows.frameId >= snap)&(receiver_rows.frameId <= release)][['frameId','x','y']]
    d = defenders[(defenders.frameId >= snap)&(defenders.frameId <= release)][['frameId','x','y','nflId']]
    if len(r) < 2 or d.empty:
        return None
    r = r.dropna(subset=['frameId','x','y']).drop_duplicates('frameId')
    d = d.dropna(subset=['frameId','x','y'])
    cross = r.merge(d, on='frameId', suffixes=('_r','_d'))
    if cross.empty:
        return None
    cross['distance'] = np.hypot(cross.x_r-cross.x_d, cross.y_r-cross.y_d)
    nearest = cross.sort_values(['frameId','distance']).drop_duplicates('frameId').sort_values('frameId')
    # Make the release-frame measurement mandatory: do not substitute an earlier frame.
    if int(nearest.iloc[-1].frameId) != release:
        return None
    observed = nearest.frameId.to_numpy(dtype=int)
    separation = nearest.distance.to_numpy(dtype=float)
    # Only count separation once routes have developed (>= SETTLE_FRAMES after snap).
    open_frames = observed[(separation >= threshold) & (observed >= snap + SETTLE_FRAMES)]
    previous_open = open_frames[open_frames < release]
    at_release = float(separation[-1])
    expired = bool(len(previous_open) and at_release < threshold)
    last_open_before_release = int(previous_open[-1]) if len(previous_open) else None
    episode = run_lengths(open_frames)
    return {
        'series': nearest[['frameId','distance','nflId','x_r','y_r','x_d','y_d']].copy(),
        'at_release_yards': at_release,
        'peak_separation_yards': float(np.max(separation)),
        'longest_window_s': max(episode, default=0)*FRAME_SECONDS,
        'total_open_s': len(open_frames)*FRAME_SECONDS,
        'closed_before_release': expired,
        'time_since_last_open_s': ((release-last_open_before_release)*FRAME_SECONDS if expired else np.nan),
        'release_nearest_defender_id': nearest.iloc[-1].nflId,
        'frames_observed': int(len(nearest)),
        'snap_frame': snap,
        'release_frame': release,
    }


def analyze_game(tracking: pd.DataFrame, plays: pd.DataFrame, routes: pd.DataFrame,
                 threshold: float, max_plays: int|None = None):
    game_id = int(tracking['gameId'].dropna().iloc[0])
    game_plays = plays[(plays.gameId == game_id) & (plays.passResult.isin(['C','I','IN']))]
    game_routes = routes[routes.gameId == game_id]
    eligible_ids = set(game_plays.playId.dropna().astype(int)) & set(game_routes.playId.dropna().astype(int))
    results = []
    detail = {}
    sampled_play_ids = sorted(eligible_ids)
    if max_plays:
        sampled_play_ids = sampled_play_ids[:max_plays]
    for play_id in sampled_play_ids:
        pg = tracking[tracking.playId == play_id]
        # A tracking event may be tagged on multiple players. Unique frame IDs suffice.
        evt = pg[['frameId','event']].dropna()
        evt_names = evt.event.astype(str).str.lower().str.strip()
        snaps = evt.loc[evt_names.eq('ball_snap'), 'frameId'].dropna()
        releases = evt.loc[evt_names.eq('pass_forward'), 'frameId'].dropna()
        if snaps.empty or releases.empty:
            continue
        snap = int(snaps.min())
        releases = releases[releases > snap]
        if releases.empty:
            continue
        release = int(releases.min())
        if release-snap < 2:
            continue
        meta = game_plays.loc[game_plays.playId == play_id].iloc[0]
        defense_team = str(meta.defensiveTeam).strip()
        offense_team = str(meta.possessionTeam).strip()
        defenders = pg[(pg.team.astype(str).str.strip() == defense_team) & (pg.nflId.notna())]
        if defenders.empty:
            continue
        for _, player in game_routes[game_routes.playId == play_id].iterrows():
            nfl_id = int(player.nflId)
            receiver_rows = pg[(pg.nflId == nfl_id)&(pg.team.astype(str).str.strip() == offense_team)]
            if receiver_rows.empty:
                continue
            res = calculate_window(receiver_rows, defenders, snap, release, threshold)
            if res is None:
                continue
            summary = {k:v for k,v in res.items() if k != 'series'}
            summary.update(gameId=game_id, playId=int(play_id), nflId=nfl_id,
                           player_name=str(player.displayName),
                           position=str(player.officialPosition),
                           pass_result=str(meta.passResult),
                           possession_team=offense_team, defensive_team=defense_team,
                           play_description=str(meta.get('playDescription','')),
                           coverage_type=str(meta.get('pff_passCoverageType','Unknown')))
            results.append(summary)
            detail[(int(play_id), nfl_id)] = res['series']
    if not results:
        raise RuntimeError('No route-running receivers with valid snap/release events were found in this game. Try another tracking file or inspect events.')
    return pd.DataFrame(results), detail


def select_story(rows: pd.DataFrame):
    expired = rows[rows.closed_before_release & (rows.longest_window_s >= 0.3)]
    if not expired.empty:
        # Dramatic but factual example: prioritize closed windows that lasted longer.
        return expired.sort_values(['longest_window_s','peak_separation_yards'], ascending=False).iloc[0], True
    return rows.sort_values(['longest_window_s','peak_separation_yards'], ascending=False).iloc[0], False


def make_graphic(rows: pd.DataFrame, details: dict, tracking: pd.DataFrame,
                 selected: pd.Series, threshold: float, out: Path):
    chosen_play = int(selected.playId)
    chosen_id = int(selected.nflId)
    pg = tracking[tracking.playId == chosen_play]
    rel = int(selected.release_frame)
    snap = int(selected.snap_frame)
    rel_frame = pg[(pg.frameId == rel) & pg.nflId.notna()]
    if rel_frame.empty:
        raise RuntimeError('Selected play has no tracked players at pass release.')
    dir_value = str(pg.playDirection.dropna().iloc[0]).lower().strip()
    def orient(x, y):
        return (120-x, FIELD_WIDTH-y) if dir_value == 'left' else (x, y)
    fig = plt.figure(figsize=(16, 9.5), facecolor=DARK)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.32, 1],
                          top=.81, bottom=.21, left=.045, right=.97, wspace=.11)
    fig.text(.05,.955, 'HUT & SEEK  /  WINDOW ALERT', fontsize=29, color=GOLD,
             fontweight='bold')
    fig.text(.05,.92, 'HOW LONG SEPARATION LASTED BEFORE THE THROW', color=CREAM, fontsize=12, weight='bold')
    story = 'WINDOW EXPIRED BEFORE RELEASE' if bool(selected.closed_before_release) else 'WINDOW AVAILABLE AT RELEASE / OR NEVER OPENED'
    fig.text(.05,.885, f"{selected.player_name}  ·  {selected.position}  ·  {selected.possession_team} vs {selected.defensive_team}",
             color=CREAM, fontsize=14)
    left_ax = fig.add_subplot(gs[0,0])
    right_ax = fig.add_subplot(gs[0,1])
    left_ax.set_facecolor(GREEN)
    right_ax.set_facecolor('#102B26')
    left_ax.add_patch(Rectangle((0,0),120,FIELD_WIDTH,facecolor=GREEN,edgecolor='none'))
    left_ax.add_patch(Rectangle((0,0),10,FIELD_WIDTH,facecolor='#153126',alpha=.9))
    left_ax.add_patch(Rectangle((110,0),10,FIELD_WIDTH,facecolor='#153126',alpha=.9))
    for x in range(10,111,10):
        left_ax.plot([x,x],[0,FIELD_WIDTH],color=CREAM,alpha=.25,linewidth=1)
        if 10<x<110:
            left_ax.text(x, 3, str(min(x-10,110-x)), color=CREAM, fontsize=10,alpha=.5,ha='center',clip_on=True)
    for y in (0, FIELD_WIDTH):
        left_ax.plot([0,120],[y,y],color=CREAM,linewidth=1.5)
    off = rel_frame[rel_frame.team == selected.possession_team]
    deff = rel_frame[rel_frame.team == selected.defensive_team]
    for frame,color,marker in [(off,PALE,'o'),(deff,PURPLE,'o')]:
        x,y=orient(frame.x.to_numpy(float),frame.y.to_numpy(float))
        left_ax.scatter(x,y,s=98,edgecolors=DARK,linewidths=.7,c=color,marker=marker,zorder=5)
    route=details[(chosen_play,chosen_id)]
    path = route[route.frameId >= max(snap,rel-18)]
    px,py=orient(path.x_r.to_numpy(float),path.y_r.to_numpy(float))
    left_ax.plot(px,py,c=GOLD,lw=3,zorder=6)
    last=route[route.frameId == rel].iloc[0]
    rx,ry=orient(float(last.x_r),float(last.y_r))
    dx,dy=orient(float(last.x_d),float(last.y_d))
    left_ax.scatter([rx],[ry],s=310,c=GOLD,edgecolors='white',linewidths=2.0,zorder=10)
    left_ax.scatter([dx],[dy],s=210,c=ORANGE,edgecolors='white',linewidths=1.4,zorder=9)
    left_ax.plot([rx,dx],[ry,dy],ls='--',color=GOLD,lw=2,zorder=7)
    left_ax.annotate(f'{selected.player_name}\n{float(selected.at_release_yards):.1f} yd separation', (rx,ry),
                     xytext=(0,17),textcoords='offset points',ha='center',color=GOLD,fontsize=10,weight='bold')
    # Ball is included only if a measured football row exists at this exact frame.
    ball=pg[(pg.frameId==rel)&(pg.team.astype(str).str.lower()=='football')]
    if not ball.empty:
        bx,by=orient(float(ball.iloc[0].x),float(ball.iloc[0].y))
        left_ax.scatter([bx],[by],marker='D',s=80,c='white',edgecolors=DARK,zorder=8)
    left_ax.set_xlim(max(0,rx-31),min(120,rx+31))
    left_ax.set_ylim(0,FIELD_WIDTH)
    left_ax.set_aspect('equal', adjustable='box')
    left_ax.set_xlabel('Field position (yards, offense left → right)',color=CREAM,fontsize=10)
    left_ax.tick_params(colors=CREAM,labelsize=9)
    left_ax.set_yticks([])
    for sp in left_ax.spines.values(): sp.set_visible(False)
    left_ax.set_title('FIELD VIEW  /  AT PASS RELEASE',loc='left',fontsize=14,weight='bold',color=CREAM,pad=13)
    candidates=rows[rows.playId==chosen_play].sort_values('at_release_yards',ascending=False)
    for _,p in candidates.iterrows():
        rs=details[(chosen_play,int(p.nflId))]
        xx=(rs.frameId.to_numpy(float)-snap)*FRAME_SECONDS
        yy=rs.distance.to_numpy(float)
        chosen=(int(p.nflId)==chosen_id)
        right_ax.plot(xx,yy,alpha=1 if chosen else .55,lw=3.1 if chosen else 1.4,
                      color=GOLD if chosen else PURPLE,label=p.player_name if chosen else None,zorder=4 if chosen else 2)
    right_ax.axhline(threshold,color=ORANGE,lw=1.8,ls='--',label=f'{threshold:g}-yd proxy threshold')
    right_ax.axvline((rel-snap)*FRAME_SECONDS,color=CREAM,lw=1.2,alpha=.85)
    right_ax.set_xlabel('Seconds after snap  →  pass release',color=CREAM,fontsize=11,labelpad=12)
    right_ax.set_ylabel('Distance to nearest defender (yd)',color=CREAM,fontsize=11)
    right_ax.tick_params(colors=CREAM)
    right_ax.grid(alpha=.13,color='white')
    right_ax.set_xlim(0,(rel-snap)*FRAME_SECONDS+0.15)
    right_ax.set_ylim(bottom=0)
    for sp in right_ax.spines.values(): sp.set_color('#386052')
    right_ax.legend(loc='upper left',facecolor='#102B26',edgecolor='#386052',labelcolor=CREAM,fontsize=9)
    right_ax.set_title('WINDOW TIMELINE  /  SNAP TO THROW',loc='left',fontsize=14,weight='bold',color=CREAM,pad=13)
    # Text along bottom is an evidence-first replay graphic, not a claim about a missed pass.
    fig.text(.05,.128,story,color=ORANGE if bool(selected.closed_before_release) else PALE,
             fontsize=15,fontweight='bold')
    fig.text(.05,.091,f"Longest continuous window: {float(selected.longest_window_s):.1f}s   |   Separation at release: {float(selected.at_release_yards):.1f} yd   |   Game {int(selected.gameId)}, Play {chosen_play}",
             color=CREAM,fontsize=11)
    fig.text(.05,.057,f"2021 NFL Weeks 1–8 • Supplied Next Gen Stats/PFF data • {threshold:g}-yd threshold is a separation proxy, NOT a confirmed open passing lane • {SAMPLE_LABEL}",
             color='#A9C0B5',fontsize=9)
    fig.savefig(out,dpi=170,bbox_inches='tight',facecolor=fig.get_facecolor())
    plt.close(fig)


def make_position_plot(rows: pd.DataFrame, threshold: float, out: Path):
    groups=rows.groupby('position',as_index=False).agg(
        sample=('nflId','size'), median_window=('longest_window_s','median'),
        expired=('closed_before_release','sum'))
    groups=groups.set_index('position').reindex(['WR','TE','RB']).dropna(subset=['sample'])
    if groups.empty: return
    fig, ax=plt.subplots(figsize=(10,5),facecolor=DARK)
    ax.set_facecolor(DARK)
    bars=ax.bar(groups.index,groups.median_window,color=[{'WR':GOLD,'TE':PURPLE,'RB':ORANGE}[p] for p in groups.index],width=.55)
    for bar,(pos,row) in zip(bars,groups.iterrows()):
        ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+.02,
                f"{row.median_window:.2f}s\nn={int(row['sample'])}",ha='center',va='bottom',color=CREAM,fontsize=12)
    ax.set_ylim(0,max(.5,groups.median_window.max()*1.5+.08))
    ax.set_ylabel('Median longest continuous separation window (seconds)',color=CREAM)
    ax.tick_params(colors=CREAM,labelsize=13)
    for spine in ax.spines.values():spine.set_visible(False)
    ax.grid(axis='y',alpha=.15,color='white')
    ax.set_axisbelow(True)
    fig.suptitle('WINDOW LIFETIME  /  BY POSITION',fontsize=20,color=GOLD,fontweight='bold')
    fig.text(.1,.01,f'{SAMPLE_LABEL} (2021) • Observed receivers on qualifying pass plays • separation ≥ {threshold:g} yd • descriptive, not league-wide',color=CREAM,fontsize=9)
    fig.tight_layout(rect=(0,.045,1,.92))
    fig.savefig(out,dpi=160,facecolor=fig.get_facecolor(),bbox_inches='tight')
    plt.close(fig)


def write_submission_readme(rows: pd.DataFrame, example: pd.Series, threshold: float, game_id: int, out: Path):
    n_games=rows.gameId.nunique()
    n_plays=len(rows[['gameId','playId']].drop_duplicates())
    n_records=len(rows)
    closed=round(100*rows.closed_before_release.mean(),1)
    status=('showed a separation window that closed before pass release' if bool(example.closed_before_release)
            else 'had one of the longest observed separation windows in the analyzed games')
    content=(
        '# HUT & SEEK — Window Alert\n\n'
        f'**HUT & SEEK: Window Alert** analyzes 2021 NFL Next Gen Stats player tracking and PFF route assignments to show broadcasters, coaches, and scouts how long WRs, TEs, and RBs maintain separation before pass release.\n'
        f'Across **{n_plays} eligible passing plays** and **{n_records} receiver-play observations** across **{n_games} game{'s' if n_games != 1 else ''}** (2021 regular season, Weeks 1–8), **{closed:.1f}%** of observed receivers created at least {threshold:g} yards of nearest-defender separation at some point between 1.0 s after the snap and pass release but were below that threshold at release; the featured replay of **{example.player_name}** (game {game_id}, play {int(example.playId)}) {status}.\n'
        f'The Python tool joins supplied play, player, PFF, and tracking CSVs, calculates 10-Hz Euclidean nearest-defender distances from snap through the `pass_forward` event (counting a window as open only from 1.0 s after the snap, so pre-snap defender cushion is excluded), and displays separation-window duration on an annotated field and timeline.\n'
        f'This is a descriptive sample of {n_games} game{"s" if n_games != 1 else ""}, the threshold is an exploratory proxy rather than a validated definition of being open, and tracking proximity alone cannot establish passing-lane visibility, quarterback reads, or causation.\n'
        'Run `pip install -r requirements.txt` and then `python window_alert.py --data /path/to/nfl-big-data-bowl-regional-event-data/data` to regenerate the results.\n\n'
        '![Window Alert tracking replay](output/window_alert.png)\n\n'
        '![Window lifetime by position](output/window_by_position.png)\n\n'
        '[Per-receiver metrics](output/window_metrics.csv) · [Per-game summary](output/game_summary.csv) · [Selected play tracking timeline](output/featured_window_timeline.csv)\n'
    )
    (out/'README.md').write_text(content,encoding='utf-8')


_WORKER = {}


def _init_worker(plays, routes, threshold, max_plays):
    _WORKER.update(plays=plays, routes=routes, threshold=threshold, max_plays=max_plays)


def _analyze_file(path: Path):
    """Analyze one game file; returns (file name, metrics DataFrame or None, message)."""
    try:
        tracking = pd.read_csv(path, low_memory=False)
        for col in ('gameId','playId','nflId','frameId','x','y'):
            tracking[col] = pd.to_numeric(tracking[col], errors='coerce')
        rows, _ = analyze_game(tracking, _WORKER['plays'], _WORKER['routes'],
                               _WORKER['threshold'], _WORKER['max_plays'])
        return path.name, rows, f'{len(rows)} receiver-play observations'
    except Exception as exc:  # one bad game should not stop the whole run
        return path.name, None, f'skipped ({exc})'


def add_game_info(rows: pd.DataFrame, data: Path) -> pd.DataFrame:
    games_path = data/'games.csv'
    if not games_path.is_file():
        return rows
    games = pd.read_csv(games_path, low_memory=False)
    games['gameId'] = pd.to_numeric(games['gameId'], errors='coerce')
    keep = [c for c in ('gameId','season','week','gameDate','homeTeamAbbr','visitorTeamAbbr') if c in games.columns]
    return rows.merge(games[keep], on='gameId', how='left')


def game_summary(rows: pd.DataFrame) -> pd.DataFrame:
    keys = [c for c in ('gameId','week','homeTeamAbbr','visitorTeamAbbr') if c in rows.columns]
    return (rows.groupby(keys, as_index=False, dropna=False)
                .agg(plays=('playId','nunique'), receiver_observations=('nflId','size'),
                     closed_before_release_pct=('closed_before_release', lambda v: round(100*v.mean(),1)),
                     median_longest_window_s=('longest_window_s','median'),
                     median_separation_at_release_yd=('at_release_yards','median'))
                .sort_values(keys[:2]))


def main():
    global SAMPLE_LABEL
    ap=argparse.ArgumentParser(description='HUT & SEEK: Window Alert — only NFL-provided dataset')
    ap.add_argument('--data',default='data',help='Path to provided data folder containing tracking/')
    ap.add_argument('--game-id',type=int,help='Analyze one exact game ID only; default analyzes EVERY tracking file')
    ap.add_argument('--max-games',type=int,default=None,help='Optional: only the first N tracking files (quick test)')
    ap.add_argument('--max-plays',type=int,default=None,help='Optional quick smoke test: first N plays per game')
    ap.add_argument('--threshold',type=float,default=3.0,help='Yard threshold for separation (default: 3)')
    ap.add_argument('--workers',type=int,default=os.cpu_count() or 1,help='Parallel processes (default: all CPU cores)')
    args=ap.parse_args()
    data=find_data_dir(args.data)
    files=sorted((data/'tracking').glob('tracking_*.csv'))
    if not files:
        raise FileNotFoundError(f'No tracking/tracking_[gameId].csv in {data}')
    if args.game_id is not None:
        path=data/'tracking'/f'tracking_{args.game_id}.csv'
        if not path.exists():
            raise FileNotFoundError(f'{path} not found. Available examples: {[p.stem for p in files[:5]]}')
        files=[path]
    elif args.max_games:
        files=files[:args.max_games]
    plays,routes=load_tables(data)
    print(f'Analyzing {len(files)} game tracking file(s) with {args.workers} worker(s)...', flush=True)
    frames=[]
    init=(plays,routes,args.threshold,args.max_plays)
    if args.workers > 1 and len(files) > 1:
        with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker, initargs=init) as pool:
            results=pool.map(_analyze_file, files)
            for n,(name,rows_i,msg) in enumerate(results,1):
                print(f'  [{n}/{len(files)}] {name}: {msg}', flush=True)
                if rows_i is not None: frames.append(rows_i)
    else:
        _init_worker(*init)
        for n,path in enumerate(files,1):
            name,rows_i,msg=_analyze_file(path)
            print(f'  [{n}/{len(files)}] {name}: {msg}', flush=True)
            if rows_i is not None: frames.append(rows_i)
    if not frames:
        raise RuntimeError('No game produced valid receiver observations.')
    rows=add_game_info(pd.concat(frames, ignore_index=True), data)
    n_games=rows.gameId.nunique()
    SAMPLE_LABEL=f"{n_games}-game sample" if n_games > 1 else 'one-game sample'
    choice,_=select_story(rows)
    # Re-load only the featured game to draw its field view and timeline.
    feat_game=int(choice.gameId)
    tracking=load_tracking(data/'tracking'/f'tracking_{feat_game}.csv')
    game_rows,details=analyze_game(tracking,plays,routes,args.threshold,args.max_plays)
    out=Path('output')
    out.mkdir(parents=True,exist_ok=True)
    rows.drop(columns=['release_nearest_defender_id'], errors='ignore').to_csv(out/'window_metrics.csv',index=False)
    game_summary(rows).to_csv(out/'game_summary.csv',index=False)
    series=details[(int(choice.playId),int(choice.nflId))]
    series.assign(seconds_since_snap=(series.frameId-int(choice.snap_frame))*FRAME_SECONDS).to_csv(out/'featured_window_timeline.csv',index=False)
    make_graphic(game_rows,details,tracking,choice,args.threshold,out/'window_alert.png')
    make_position_plot(rows,args.threshold,out/'window_by_position.png')
    write_submission_readme(rows,choice,args.threshold,feat_game,Path('.'))
    n_plays=len(rows[['gameId','playId']].drop_duplicates())
    print('\n=== REAL DATA RESULTS ===')
    print(f'Games analyzed: {n_games} | plays analyzed: {n_plays} | receiver-play observations: {len(rows)}')
    print(f'Previously open, then closed at release: {int(rows.closed_before_release.sum())}/{len(rows)} ({rows.closed_before_release.mean():.1%})')
    print(f'Featured: {choice.player_name} ({choice.position}), game {feat_game}, play {int(choice.playId)}')
    print(f'Longest open window: {float(choice.longest_window_s):.1f} sec | separation at release: {float(choice.at_release_yards):.2f} yd')
    print('Created README.md and output/window_alert.png, output/window_by_position.png, output/window_metrics.csv, output/game_summary.csv')
    print('IMPORTANT: Verify outputs, then push Python code, README.md and output PNGs to your PUBLIC GitHub repo.')

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        raise SystemExit(1)
