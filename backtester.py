"""
╔══════════════════════════════════════════════════════════════╗
║           BACKTESTER V8 — ADAPTATIF                          ║
║   Test de la couche intelligente de détection de régime      ║
║   RANGE → RSI 30/70 | TENDANCE → RSI 25/75                  ║
║   VOLATILE → Mise 10% | 10 marchés | H1                     ║
╚══════════════════════════════════════════════════════════════╝
"""

import requests
import pandas as pd
import time
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator

# ══════════════════════════════════════════════════════════════
# PARAMÈTRES — IDENTIQUES V8
# ══════════════════════════════════════════════════════════════

CAPITAL_INITIAL  = 50.0
LEVIER           = 10
MISE_PCT_NORMAL  = 0.20
MISE_PCT_VOLATILE= 0.10
ATR_MULTIPLIER   = 2.5
RATIO_RR         = 2.0
RATIO_PARTIEL    = 1.0
VOLUME_MINI      = 0.40
ADX_MAX          = 40
FRAIS_PCT        = 0.0004
SLIPPAGE_PCT     = 0.0002
TIMEOUT_BOUGIES  = 12

# Seuils V8
V8_ADX_TENDANCE  = 35
V8_ATR_VOLATILE  = 3.0

# RSI selon régime
RSI_RANGE        = (30, 70)   # Normal
RSI_TENDANCE     = (25, 75)   # Plus strict en tendance

MARCHES = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "ATOMUSDT", "LINKUSDT",
    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT"
]

KRAKEN_SYMBOLS = {
    "BTCUSDT":  "XXBTZUSD", "ETHUSDT":  "XETHZUSD",
    "XRPUSDT":  "XXRPZUSD", "ATOMUSDT": "ATOMUSD",
    "LINKUSDT": "LINKUSD",  "ADAUSDT":  "ADAUSD",
    "SOLUSDT":  "SOLUSD",   "AVAXUSDT": "AVAXUSD",
    "NEARUSDT": "NEARUSD",  "DOTUSDT":  "DOTUSD"
}

# ══════════════════════════════════════════════════════════════
# DONNÉES
# ══════════════════════════════════════════════════════════════

def get_data(symbole):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url    = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": kraken_symbol, "interval": 60}
    print(f"  Téléchargement {symbole}...")
    try:
        r    = requests.get(url, params=params, timeout=30)
        data = r.json()
        if data.get("error") and data["error"]:
            print(f"  Erreur API {symbole} : {data['error']}")
            return None
        result = data.get("result", {})
        keys   = [k for k in result.keys() if k != "last"]
        if not keys:
            return None
        candles = result[keys[0]]
        df = pd.DataFrame(candles, columns=[
            'time','open','high','low','close','vwap','volume','count'
        ])
        df = df.astype({'time': int, 'high': float, 'low': float,
                        'close': float, 'volume': float})
        df['datetime'] = pd.to_datetime(df['time'], unit='s')
        df = df.set_index('datetime').sort_index()
        print(f"  {len(df)} bougies H1")
        return df
    except Exception as e:
        print(f"  Erreur {symbole} : {e}")
        return None

def ajouter_indicateurs(df):
    df = df.copy()
    df['adx'] = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14).adx()
    df['atr'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
    df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()
    df['vol_moy']   = df['volume'].rolling(24).mean()
    df['vol_ratio'] = df['volume'] / df['vol_moy']
    return df.dropna()

# ══════════════════════════════════════════════════════════════
# DÉTECTION RÉGIME — IDENTIQUE V8
# ══════════════════════════════════════════════════════════════

def detecter_regime_bougie(df, i):
    """Détecte le régime à la bougie i basé sur ADX et ATR."""
    try:
        adx = float(df['adx'].iloc[i])
        atr = float(df['atr'].iloc[i])
        prix = float(df['close'].iloc[i])
        atr_pct = (atr / prix) * 100

        if atr_pct > V8_ATR_VOLATILE:
            return "VOLATILE", 0.10, 30, 70
        elif adx > V8_ADX_TENDANCE:
            return "TENDANCE", 0.20, 25, 75
        else:
            return "RANGE", 0.20, 30, 70
    except:
        return "RANGE", 0.20, 30, 70

# ══════════════════════════════════════════════════════════════
# BACKTEST V8
# ══════════════════════════════════════════════════════════════

def backtest_v8(df, symbole):
    trades   = []
    capital  = CAPITAL_INITIAL
    i        = 20
    frais    = (FRAIS_PCT + SLIPPAGE_PCT) * 2

    nb_range    = 0
    nb_tendance = 0
    nb_volatile = 0

    while i < len(df) - TIMEOUT_BOUGIES - 2:
        row = df.iloc[i]

        if row['vol_ratio'] < VOLUME_MINI:
            i += 1
            continue
        if row['adx'] > ADX_MAX:
            i += 1
            continue

        prix   = row['close']
        atr    = row['atr']
        rsi    = row['rsi']

        # Détection régime V8
        regime, mise_pct, rsi_achat, rsi_vente = detecter_regime_bougie(df, i)

        if regime == "RANGE":    nb_range += 1
        if regime == "TENDANCE": nb_tendance += 1
        if regime == "VOLATILE": nb_volatile += 1

        signal = None
        if rsi < rsi_achat:
            signal = "ACHAT"
        elif rsi > rsi_vente:
            signal = "VENTE"

        if signal is None:
            i += 1
            continue

        mise          = CAPITAL_INITIAL * mise_pct
        distance_stop = atr * ATR_MULTIPLIER

        if signal == "ACHAT":
            stop_loss   = prix - distance_stop
            obj_partiel = prix + (distance_stop * RATIO_PARTIEL)
            obj_final   = prix + (distance_stop * RATIO_RR)
        else:
            stop_loss   = prix + distance_stop
            obj_partiel = prix - (distance_stop * RATIO_PARTIEL)
            obj_final   = prix - (distance_stop * RATIO_RR)

        partiel_fait = False
        gain_partiel = 0
        gain_brut    = 0
        resultat     = "NEUTRE"
        duree        = 0

        for j in range(i + 1, min(i + TIMEOUT_BOUGIES + 1, len(df))):
            row_j = df.iloc[j]
            duree = j - i

            if signal == "ACHAT":
                if not partiel_fait and row_j['high'] >= obj_partiel:
                    gain_partiel = (obj_partiel - prix) / prix * mise * LEVIER * 0.5
                    partiel_fait = True
                    stop_loss    = prix
                if row_j['low'] <= stop_loss:
                    gain_reste = (stop_loss - prix) / prix * mise * LEVIER * (0.5 if partiel_fait else 1.0)
                    gain_brut  = gain_partiel + gain_reste
                    resultat   = "GAGNE" if gain_brut > 0 else "PERDU"
                    break
                if row_j['high'] >= obj_final:
                    gain_reste = (obj_final - prix) / prix * mise * LEVIER * 0.5
                    gain_brut  = gain_partiel + gain_reste
                    resultat   = "GAGNE"
                    break
            else:
                if not partiel_fait and row_j['low'] <= obj_partiel:
                    gain_partiel = (prix - obj_partiel) / prix * mise * LEVIER * 0.5
                    partiel_fait = True
                    stop_loss    = prix
                if row_j['high'] >= stop_loss:
                    gain_reste = (prix - stop_loss) / prix * mise * LEVIER * (0.5 if partiel_fait else 1.0)
                    gain_brut  = gain_partiel + gain_reste
                    resultat   = "GAGNE" if gain_brut > 0 else "PERDU"
                    break
                if row_j['low'] <= obj_final:
                    gain_reste = (prix - obj_final) / prix * mise * LEVIER * 0.5
                    gain_brut  = gain_partiel + gain_reste
                    resultat   = "GAGNE"
                    break

        if resultat == "NEUTRE":
            prix_fin  = df.iloc[min(i + TIMEOUT_BOUGIES, len(df)-1)]['close']
            if signal == "ACHAT":
                g = (prix_fin - prix) / prix * mise * LEVIER
            else:
                g = (prix - prix_fin) / prix * mise * LEVIER
            gain_brut = gain_partiel + g * (0.5 if partiel_fait else 1.0)
            resultat  = "GAGNE" if gain_brut > 0 else "PERDU"
            duree     = TIMEOUT_BOUGIES

        gain_net = round(gain_brut - frais * mise * LEVIER, 2)
        capital  = round(capital + gain_net, 2)

        trades.append({
            'date':    df.index[i].strftime('%Y-%m-%d %H:%M'),
            'signal':  signal,
            'regime':  regime,
            'resultat':resultat,
            'gain':    gain_net,
            'duree_h': duree,
            'capital': capital,
            'adx':     round(row['adx'], 1),
            'rsi':     round(rsi, 1),
            'atr_pct': round((atr / prix) * 100, 2),
            'mise_pct':mise_pct * 100
        })

        i = i + duree + 2

    print(f"\n  Signaux RANGE    : {nb_range}")
    print(f"  Signaux TENDANCE : {nb_tendance}")
    print(f"  Signaux VOLATILE : {nb_volatile}")

    return trades, capital

# ══════════════════════════════════════════════════════════════
# AFFICHAGE
# ══════════════════════════════════════════════════════════════

def afficher(trades, capital_final, symbole):
    if not trades:
        print(f"  Aucun trade sur {symbole}")
        return None

    df_t     = pd.DataFrame(trades)
    nb       = len(trades)
    wins     = len(df_t[df_t['resultat'] == 'GAGNE'])
    losses   = len(df_t[df_t['resultat'] == 'PERDU'])
    win_rate = wins / nb * 100
    gain_tot = df_t['gain'].sum()
    perf     = (capital_final - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    avg_win  = df_t[df_t['resultat']=='GAGNE']['gain'].mean() if wins > 0 else 0
    avg_loss = df_t[df_t['resultat']=='PERDU']['gain'].mean() if losses > 0 else 0

    # Par régime
    for regime in ['RANGE', 'TENDANCE', 'VOLATILE']:
        r_trades = df_t[df_t['regime'] == regime]
        if len(r_trades) > 0:
            r_wins = len(r_trades[r_trades['resultat'] == 'GAGNE'])
            r_wr   = r_wins / len(r_trades) * 100
            r_gain = r_trades['gain'].sum()

    capitals = [CAPITAL_INITIAL] + [t['capital'] for t in trades]
    peak = CAPITAL_INITIAL
    max_dd = 0
    for c in capitals:
        if c > peak: peak = c
        dd = (c - peak) / peak * 100
        if dd < max_dd: max_dd = dd

    jours = (pd.to_datetime(trades[-1]['date']) - pd.to_datetime(trades[0]['date'])).days
    sem   = nb / max(jours / 7, 1)

    print(f"\n  {'='*55}")
    print(f"  RÉSULTATS V8 — {symbole}")
    print(f"  {'='*55}")
    print(f"  Période        : {jours} jours")
    print(f"  Trades         : {nb} ({round(sem,1)}/semaine)")
    print(f"  Victoires      : {wins} ({round(win_rate,1)}%)")
    print(f"  Défaites       : {losses}")
    print(f"  Gain moyen win : +{round(avg_win,2)}EUR")
    print(f"  Perte moyenne  : {round(avg_loss,2)}EUR")
    print(f"  Capital final  : {round(capital_final,2)}EUR")
    print(f"  Performance    : {'+' if perf>=0 else ''}{round(perf,1)}%")
    print(f"  Gain total net : {'+' if gain_tot>=0 else ''}{round(gain_tot,2)}EUR")
    print(f"  Drawdown max   : {round(max_dd,1)}%")
    print(f"  {'─'*55}")

    for regime in ['RANGE', 'TENDANCE', 'VOLATILE']:
        r_trades = df_t[df_t['regime'] == regime]
        if len(r_trades) > 0:
            r_wins = len(r_trades[r_trades['resultat'] == 'GAGNE'])
            r_wr   = r_wins / len(r_trades) * 100
            r_gain = r_trades['gain'].sum()
            print(f"  {regime:10} : {len(r_trades):3} trades | WR {round(r_wr,1):5}% | "
                  f"Gain {'+' if r_gain>=0 else ''}{round(r_gain,2)}EUR")

    print(f"  {'='*55}")

    print(f"\n  Tous les trades :")
    for t in trades:
        icone = "✅" if t['resultat'] == "GAGNE" else "❌"
        print(f"    {icone} {t['date']} | [{t['regime']:8}] {t['signal']:5} | "
              f"RSI {t['rsi']:5} | ADX {t['adx']:5} | "
              f"Mise {t['mise_pct']}% | "
              f"{'+' if t['gain']>=0 else ''}{t['gain']}EUR | "
              f"{t['duree_h']}h | {t['capital']}EUR")

    return {
        'symbole': symbole, 'nb_trades': nb,
        'trades_semaine': round(sem, 1),
        'win_rate': round(win_rate, 1),
        'performance': round(perf, 1),
        'gain_total': round(gain_tot, 2),
        'drawdown_max': round(max_dd, 1)
    }

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 55)
    print("  BACKTESTER V8 — ADAPTATIF")
    print(f"  RANGE    → RSI {RSI_RANGE} | Mise {MISE_PCT_NORMAL*100}%")
    print(f"  TENDANCE → RSI {RSI_TENDANCE} | Mise {MISE_PCT_NORMAL*100}%")
    print(f"  VOLATILE → ATR > {V8_ATR_VOLATILE}% | Mise {MISE_PCT_VOLATILE*100}%")
    print(f"  Stop ATR×{ATR_MULTIPLIER} | Ratio 1:{RATIO_RR} | Partiel 50%")
    print(f"  {len(MARCHES)} marchés")
    print("=" * 55)

    resultats = []
    for symbole in MARCHES:
        df = get_data(symbole)
        if df is None or len(df) < 50:
            print(f"  Skip {symbole}")
            continue
        df = ajouter_indicateurs(df)
        print(f"  {len(df)} bougies valides")
        trades, capital = backtest_v8(df, symbole)
        result = afficher(trades, capital, symbole)
        if result:
            resultats.append(result)
        time.sleep(1)

    if resultats:
        print(f"\n  {'='*55}")
        print(f"  SYNTHÈSE GLOBALE V8")
        print(f"  {'='*55}")
        print(f"  {'Marché':10} | {'T':3} | {'T/s':4} | {'WR':6} | {'Perf':7} | {'DD':5}")
        print(f"  {'-'*55}")
        for r in sorted(resultats, key=lambda x: x['performance'], reverse=True):
            print(f"  {r['symbole']:10} | {r['nb_trades']:3} | "
                  f"{r['trades_semaine']:4} | {r['win_rate']:5}% | "
                  f"{'+' if r['performance']>=0 else ''}{r['performance']:5}% | "
                  f"{r['drawdown_max']:4}%")
        print(f"  {'='*55}")
        meilleur = max(resultats, key=lambda x: x['performance'])
        print(f"\n  Meilleur marché : {meilleur['symbole']} → {meilleur['performance']}%")

if __name__ == "__main__":
    main()
