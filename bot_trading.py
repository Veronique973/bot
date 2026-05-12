"""
╔══════════════════════════════════════════════════════════════╗
║           BOT MEAN REVERSION V8 — ADAPTATIF                 ║
║   Protection DÈS +0,75€ de PnL                             ║
║   Paliers : 0.75€, 3€, 7.50€, 12€, 18€, 25€, 35€, 50€...    ║
║   PnL / Multiplicateur / Protège affichés                   ║
╚══════════════════════════════════════════════════════════════╝
"""

import requests
import time
import os
import logging
import pandas as pd
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from datetime import datetime
from database import init_database, charger_etat, sauvegarder_etat, enregistrer_trade

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

CAPITAL_INITIAL         = 215.0
LEVIER                  = 10
MISE_FIXE_PCT           = 0.20
KELLY_FRACTION          = 0.25
KELLY_CAP               = 0.20
MIN_TRADES_KELLY        = 30
ATR_MULTIPLIER          = 2.5
RATIO_RR                = 2.0
RATIO_PARTIEL           = 1.0
PAUSE                   = 120
CHECK_INTERVAL          = 10
TIMEOUT_TRADE           = 12 * 3600
RSI_ACHAT               = 30
RSI_VENTE               = 70
VOLUME_MINI             = 0.40
ADX_MAX                 = 40
MAX_PERTES_CONSECUTIVES = 2
SEUIL_RUINE             = 0.30
PAUSE_DUREE             = 43200      # 12h

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# ========== NOUVEAUX Paliers : protection DÈS +0.75€ ==========
TRAILING_NIVEAUX = [
    (100, 0.05),   # PnL ≥ +100€  → ATR × 0.05
    ( 75, 0.07),   # PnL ≥ +75€   → ATR × 0.07
    ( 50, 0.10),   # PnL ≥ +50€   → ATR × 0.10
    ( 35, 0.15),   # PnL ≥ +35€   → ATR × 0.15
    ( 25, 0.20),   # PnL ≥ +25€   → ATR × 0.20
    ( 18, 0.30),   # PnL ≥ +18€   → ATR × 0.30
    ( 12, 0.50),   # PnL ≥ +12€   → ATR × 0.50
    (7.5, 0.80),   # PnL ≥ +7.50€ → ATR × 0.80
    (  3, 1.50),   # PnL ≥ +3€    → ATR × 1.50
    (0.75, 2.00),  # PnL ≥ +0.75€ → ATR × 2.00 (protection douce)
    (  0, 2.50),   # défaut        → ATR × 2.50
]

def get_multiplicateur_atr(pnl):
    for seuil, mult in TRAILING_NIVEAUX:
        if pnl >= seuil:
            return mult
    return 2.50

MARCHES = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "ATOMUSDT", "LINKUSDT",
    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT"
]

KRAKEN_SYMBOLS = {
    "BTCUSDT":  "XXBTZUSD",
    "ETHUSDT":  "XETHZUSD",
    "XRPUSDT":  "XXRPZUSD",
    "ATOMUSDT": "ATOMUSD",
    "LINKUSDT": "LINKUSD",
    "ADAUSDT":  "ADAUSD",
    "SOLUSDT":  "SOLUSD",
    "AVAXUSDT": "AVAXUSD",
    "NEARUSDT": "NEARUSD",
    "DOTUSDT":  "DOTUSD"
}

# ══════════════════════════════════════════════════════════════
# V8 — DÉTECTION DU RÉGIME DE MARCHÉ
# ══════════════════════════════════════════════════════════════

V8_ADX_TENDANCE    = 25
V8_ATR_VOLATILE    = 3.0
V8_SCAN_INTERVAL   = 3600

regime_actuel = {
    "mode": "RANGE",
    "mise_pct": 0.20,
    "rsi_achat": 27,
    "rsi_vente": 73,
    "derniere_maj": 0
}

def detecter_regime():
    adx_values = []
    atr_pct_values = []

    for symbole in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        df = get_klines(symbole, limite=50)
        if df is None or len(df) < 20:
            continue
        try:
            adx_ind = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
            atr_ind = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14)
            adx_val = float(adx_ind.adx().iloc[-1])
            atr_val = float(atr_ind.average_true_range().iloc[-1])
            prix    = float(df['close'].iloc[-1])
            atr_pct = (atr_val / prix) * 100
            if not pd.isna(adx_val):
                adx_values.append(adx_val)
            if not pd.isna(atr_pct):
                atr_pct_values.append(atr_pct)
        except:
            pass
        time.sleep(0.5)

    if not adx_values:
        return

    adx_moyen = sum(adx_values) / len(adx_values)
    atr_moyen = sum(atr_pct_values) / len(atr_pct_values) if atr_pct_values else 0

    ancien_mode = regime_actuel["mode"]

    if atr_moyen > V8_ATR_VOLATILE:
        regime_actuel["mode"]      = "VOLATILE"
        regime_actuel["mise_pct"]  = 0.10
        regime_actuel["rsi_achat"] = 27
        regime_actuel["rsi_vente"] = 73
    elif adx_moyen > V8_ADX_TENDANCE:
        regime_actuel["mode"]      = "TENDANCE"
        regime_actuel["mise_pct"]  = 0.20
        regime_actuel["rsi_achat"] = 25
        regime_actuel["rsi_vente"] = 75
    else:
        regime_actuel["mode"]      = "RANGE"
        regime_actuel["mise_pct"]  = 0.20
        regime_actuel["rsi_achat"] = 27
        regime_actuel["rsi_vente"] = 73

    regime_actuel["derniere_maj"] = int(time.time())

    log.info(f"\n  {'='*55}")
    log.info(f"  [V8] RÉGIME DÉTECTÉ : {regime_actuel['mode']}")
    log.info(f"  ADX moyen : {round(adx_moyen,1)} | ATR moyen : {round(atr_moyen,2)}%")
    log.info(f"  Mise : {regime_actuel['mise_pct']*100}% | "
             f"RSI : {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}")
    log.info(f"  {'='*55}")

    if ancien_mode != regime_actuel["mode"]:
        emoji = "📊" if regime_actuel["mode"] == "RANGE" else \
                "⚡" if regime_actuel["mode"] == "VOLATILE" else "📈"
        telegram(f"{emoji} <b>CHANGEMENT DE RÉGIME</b>\n"
                 f"{ancien_mode} → <b>{regime_actuel['mode']}</b>\n"
                 f"ADX : {round(adx_moyen,1)} | ATR : {round(atr_moyen,2)}%\n"
                 f"Mise : {regime_actuel['mise_pct']*100}% | "
                 f"RSI : {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}")

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")

def get_prix_actuel(symbole):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/Ticker"
    try:
        r = requests.get(url, params={"pair": kraken_symbol}, timeout=10)
        data = r.json()
        if data.get("error") and data["error"]:
            return None
        result = data.get("result", {})
        if not result:
            return None
        key = list(result.keys())[0]
        return float(result[key]["c"][0])
    except Exception as e:
        log.error(f"Erreur prix {symbole} : {e}")
        return None

def get_klines(symbole, limite=100):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": kraken_symbol, "interval": 60}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        errors = data.get("error", [])
        if errors:
            return None
        result = data.get("result", {})
        keys = [k for k in result.keys() if k != "last"]
        if not keys:
            return None
        candles = result[keys[0]]
        df = pd.DataFrame(candles, columns=[
            'time','open','high','low','close','vwap','volume','count'
        ])
        df = df.astype({'high': float, 'low': float, 'close': float, 'volume': float})
        return df.tail(limite).reset_index(drop=True)
    except Exception as e:
        log.error(f"Erreur klines {symbole} : {e}")
        return None

def get_tendance_btc():
    try:
        kraken_symbol = "XXBTZUSD"
        url = "https://api.kraken.com/0/public/OHLC"
        r = requests.get(url, params={"pair": kraken_symbol, "interval": 15}, timeout=10)
        data = r.json()
        if data.get("error") and data["error"]:
            return "NEUTRE"
        result = data.get("result", {})
        keys = [k for k in result.keys() if k != "last"]
        if not keys:
            return "NEUTRE"
        candles = result[keys[0]]
        df = pd.DataFrame(candles, columns=[
            'time','open','high','low','close','vwap','volume','count'
        ])
        df = df.astype({'close': float})
        df = df.tail(10).reset_index(drop=True)
        prix_actuel    = float(df['close'].iloc[-1])
        prix_30m_avant = float(df['close'].iloc[-3])
        variation      = (prix_actuel - prix_30m_avant) / prix_30m_avant * 100
        if variation > 1.0:
            return "HAUSSE"
        elif variation < -1.0:
            return "BAISSE"
        else:
            return "NEUTRE"
    except:
        return "NEUTRE"

def calculer_adx(df, periode=14):
    try:
        ind = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=periode)
        val = ind.adx().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 0
    except:
        return 0

def calculer_atr(df, periode=14):
    try:
        ind = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=periode)
        val = ind.average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0
    except:
        return 0

def calculer_rsi(df, periode=14):
    try:
        ind = RSIIndicator(close=df['close'], window=periode)
        val = ind.rsi().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 50
    except:
        return 50

def verifier_volume(df):
    volumes = df['volume'].tolist()
    if len(volumes) < 10:
        return True, 0
    moyenne_24h   = sum(volumes[-24:]) / len(volumes[-24:])
    volume_recent = volumes[-1]
    ratio = volume_recent / moyenne_24h if moyenne_24h > 0 else 0
    return ratio >= VOLUME_MINI, round(ratio * 100, 1)

def analyser_marche(symbole):
    df = get_klines(symbole, limite=100)
    if df is None or len(df) < 30:
        log.warning(f"  {symbole} : données insuffisantes")
        return "NEUTRE", {}
    adx = calculer_adx(df)
    atr = calculer_atr(df)
    rsi = calculer_rsi(df)
    volume_ok, volume_ratio = verifier_volume(df)
    if not volume_ok:
        log.info(f"  {symbole} : Volume {volume_ratio}% < {VOLUME_MINI*100}% → skip")
        return "NEUTRE", {}
    prix    = df['close'].iloc[-1]
    atr_pct = (atr / prix) * 100
    if adx > ADX_MAX:
        log.info(f"  {symbole} : ADX {adx} > {ADX_MAX} → skip")
        return "NEUTRE", {}
    details = {
        "adx": adx, "atr": atr, "rsi": rsi,
        "atr_pct": atr_pct, "volume_ratio": volume_ratio,
        "df": df
    }
    rsi_achat = regime_actuel["rsi_achat"]
    rsi_vente = regime_actuel["rsi_vente"]
    if rsi < rsi_achat:
        log.info(f"  {symbole} : RSI {rsi} < {rsi_achat} → SURVENDU → ACHAT ✅ "
                 f"(ADX {adx} | Vol {volume_ratio}% | ATR {round(atr_pct,2)}%) "
                 f"[Mode {regime_actuel['mode']}]")
        return "ACHAT", details
    elif rsi > rsi_vente:
        log.info(f"  {symbole} : RSI {rsi} > {rsi_vente} → SURACHETÉ → VENTE ✅ "
                 f"(ADX {adx} | Vol {volume_ratio}% | ATR {round(atr_pct,2)}%) "
                 f"[Mode {regime_actuel['mode']}]")
        return "VENTE", details
    else:
        log.info(f"  {symbole} : RSI {rsi} | ADX {adx} → pas de signal")
        return "NEUTRE", details

def choisir_meilleur_marche():
    log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan V8 — {len(MARCHES)} marchés "
             f"[Mode : {regime_actuel['mode']} | "
             f"Mise {regime_actuel['mise_pct']*100}% | "
             f"RSI {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}]")
    signaux = {}
    for marche in MARCHES:
        direction, details = analyser_marche(marche)
        if direction != "NEUTRE":
            signaux[marche] = {"direction": direction, "details": details}
        time.sleep(0.5)
    if not signaux:
        log.info("  => Aucun signal. On attend...")
        return None, "NEUTRE", {}
    tendance_btc = get_tendance_btc()
    signaux_filtres = {}
    for marche, data in signaux.items():
        direction = data["direction"]
        if tendance_btc == "HAUSSE" and direction == "VENTE":
            continue
        if tendance_btc == "BAISSE" and direction == "ACHAT":
            continue
        signaux_filtres[marche] = data
    if not signaux_filtres:
        log.info("  => Aucun signal après filtrage BTC. On attend...")
        return None, "NEUTRE", {}
    meilleur = max(signaux_filtres.items(),
                   key=lambda x: (abs(x[1]["details"].get("rsi", 50) - 50),
                                  x[1]["details"].get("atr_pct", 0)))[0]
    direction = signaux_filtres[meilleur]["direction"]
    rsi       = signaux_filtres[meilleur]["details"].get("rsi", 50)
    adx       = signaux_filtres[meilleur]["details"].get("adx", 0)
    atr_pct   = signaux_filtres[meilleur]["details"].get("atr_pct", 0)
    log.info(f"\n  => MEILLEUR SIGNAL : {meilleur} ({direction})")
    log.info(f"     RSI {rsi} | ADX {adx} | ATR {round(atr_pct,2)}%")
    autres = [m for m in signaux_filtres if m != meilleur]
    if autres:
        log.info(f"     Autres signaux : {', '.join(autres)}")
    return meilleur, direction, signaux_filtres[meilleur]["details"]

def calculer_mise(capital, nb_trades, win_rate, avg_win_pct, avg_loss_pct):
    mise_pct = regime_actuel["mise_pct"]
    if nb_trades < MIN_TRADES_KELLY:
        mise = capital * mise_pct
    else:
        if avg_loss_pct <= 0:
            mise = capital * mise_pct
        else:
            b          = avg_win_pct / avg_loss_pct
            p          = win_rate
            q          = 1 - p
            kelly_full = (p * b - q) / b
            kelly_frac = kelly_full * KELLY_FRACTION
            kelly_frac = max(0, min(kelly_frac, KELLY_CAP))
            mise       = capital * kelly_frac
    mise = max(mise, 5.0)
    mise = min(mise, capital * 0.30)
    return round(mise, 2)

def simuler_trade(symbole, direction, numero_trade, capital, details, etat):
    prix_entree = get_prix_actuel(symbole)
    if prix_entree is None:
        return "ERREUR", 0, 0, {}
    atr = details.get("atr", 0)
    if direction == "ACHAT":
        stop_loss        = round(prix_entree - (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final   = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_RR), 8)
    else:
        stop_loss        = round(prix_entree + (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final   = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_RR), 8)
    distance_stop     = abs(prix_entree - stop_loss)
    distance_stop_pct = (distance_stop / prix_entree) * 100
    win_rate = etat["nb_wins"] / etat["nb_trades"] if etat["nb_trades"] > 0 else 0.50
    avg_win  = etat["avg_win_pct"] if etat["avg_win_pct"] > 0 else distance_stop_pct * RATIO_RR
    avg_loss = etat["avg_loss_pct"] if etat["avg_loss_pct"] > 0 else distance_stop_pct
    mise     = calculer_mise(capital, etat["nb_trades"], win_rate, avg_win, avg_loss)
    log.info(f"\n  {'='*50}")
    log.info(f"  TRADE #{numero_trade} [V8-{regime_actuel['mode']}] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {'='*50}")
    log.info(f"  Symbole          : {symbole} ({direction})")
    log.info(f"  Régime V8        : {regime_actuel['mode']}")
    log.info(f"  RSI              : {details.get('rsi', 0)}")
    log.info(f"  Prix entree      : {prix_entree}")
    log.info(f"  Stop ATR×{ATR_MULTIPLIER}     : {stop_loss} ({round(distance_stop_pct,2)}%)")
    log.info(f"  Objectif partiel : {objectif_partiel}")
    log.info(f"  Objectif final   : {objectif_final}")
    log.info(f"  Mise             : {mise}EUR ({regime_actuel['mise_pct']*100}%) | Levier x{LEVIER}")
    log.info(f"  Trailing stop    : PROGRESSIF (11 niveaux, protection dès +0.75€)\n")
    telegram(f"📊 <b>TRADE #{numero_trade} OUVERT [V8]</b>\n"
             f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
             f"Mode : {regime_actuel['mode']}\n"
             f"RSI : {details.get('rsi', 0)}\n"
             f"Prix : {prix_entree}\n"
             f"Stop : {stop_loss} ({round(distance_stop_pct,2)}%)\n"
             f"Objectif : {objectif_final}\n"
             f"Mise : {mise}€ × x{LEVIER}")
    debut           = time.time()
    stop_actuel     = stop_loss
    meilleur_prix   = prix_entree
    dernier_log     = 0
    prix_sortie     = prix_entree
    partiel_execute = False
    gain_partiel    = 0
    niveau_actuel   = 2.50
    while True:
        time.sleep(CHECK_INTERVAL)
        prix_actuel = get_prix_actuel(symbole)
        if prix_actuel is None:
            continue
        prix_sortie = prix_actuel
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)
        multiplicateur    = get_multiplicateur_atr(pnl)
        distance_trailing = atr * multiplicateur
        if direction == "ACHAT":
            if prix_actuel > meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix - distance_trailing, 8)
            if nouveau_stop > stop_actuel:
                if multiplicateur != niveau_actuel:
                    gain_protege = round((nouveau_stop - prix_entree) / prix_entree * mise * LEVIER, 2)
                    log.info(f"  [TRAILING] PnL {'+' if pnl>=0 else ''}{pnl}€ → ATR×{multiplicateur} | Stop : {nouveau_stop} | Protège : ~{gain_protege}€")
                    niveau_actuel = multiplicateur
                stop_actuel = nouveau_stop
            atteint_partiel = not partiel_execute and prix_actuel >= objectif_partiel
            atteint_final   = prix_actuel >= objectif_final
            atteint_stop    = prix_actuel <= stop_actuel
        else:
            if prix_actuel < meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix + distance_trailing, 8)
            if nouveau_stop < stop_actuel:
                if multiplicateur != niveau_actuel:
                    gain_protege = round((prix_entree - nouveau_stop) / prix_entree * mise * LEVIER, 2)
                    log.info(f"  [TRAILING] PnL {'+' if pnl>=0 else ''}{pnl}€ → ATR×{multiplicateur} | Stop : {nouveau_stop} | Protège : ~{gain_protege}€")
                    niveau_actuel = multiplicateur
                stop_actuel = nouveau_stop
            atteint_partiel = not partiel_execute and prix_actuel <= objectif_partiel
            atteint_final   = prix_actuel <= objectif_final
            atteint_stop    = prix_actuel >= stop_actuel
        duree = int((time.time() - debut) / 60)
        if time.time() - dernier_log >= 60:
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole}: {prix_actuel} | "
                     f"PnL: {'+' if pnl >= 0 else ''}{pnl}EUR | "
                     f"Stop: {stop_actuel} (ATR×{multiplicateur}) | {duree}min"
                     f"{' | PARTIEL ✅' if partiel_execute else ''}")
            dernier_log = time.time()
        trade_info = {
            "prix_entree":   prix_entree,
            "prix_sortie":   prix_sortie,
            "stop_loss":     stop_loss,
            "objectif":      objectif_final,
            "duree_minutes": duree
        }
        if atteint_partiel:
            gain_partiel    = round(pnl * 0.5, 2)
            partiel_execute = True
            log.info(f"  SORTIE PARTIELLE 50% ! +{gain_partiel}EUR ✅")
            telegram(f"⚡ <b>SORTIE PARTIELLE</b>\n{symbole} | +{gain_partiel}€ sécurisés")
            continue
        if atteint_final:
            gain_final = round(pnl * 0.5, 2) if partiel_execute else pnl
            gain_total = round(gain_partiel + gain_final, 2)
            log.info(f"\n  OBJECTIF FINAL ! Total: +{gain_total}EUR 🎉")
            telegram(f"🎯 <b>OBJECTIF ATTEINT !</b>\n{symbole} {direction}\nGain : <b>+{gain_total}€</b>\nDurée : {duree} min")
            return "GAGNE", gain_total, mise, trade_info
        if atteint_stop:
            if partiel_execute:
                gain_reste = round(pnl * 0.5, 2)
                gain_total = round(gain_partiel + gain_reste, 2)
                resultat   = "GAGNE" if gain_total > 0 else "PERDU"
                log.info(f"\n  STOP (après partiel) — Total: {'+' if gain_total>=0 else ''}{gain_total}EUR")
                telegram(f"🛑 <b>STOP (après partiel)</b>\n{symbole} | {'+' if gain_total>=0 else ''}{gain_total}€\nDurée : {duree} min")
                return resultat, gain_total, mise, trade_info
            else:
                log.info(f"\n  STOP-LOSS ! {pnl}EUR")
                telegram(f"🛑 <b>STOP-LOSS</b>\n{symbole} {direction}\nPerte : <b>{pnl}€</b>\nDurée : {duree} min")
                return "PERDU", pnl, mise, trade_info
        if time.time() - debut >= TIMEOUT_TRADE:
            if partiel_execute:
                gain_reste = round(pnl * 0.5, 2)
                gain_total = round(gain_partiel + gain_reste, 2)
            else:
                gain_total = pnl
            resultat = "GAGNE" if gain_total > 0 else "PERDU"
            log.info(f"\n  TIMEOUT — Fermeture : {'+' if gain_total>=0 else ''}{gain_total}EUR")
            telegram(f"⏱ <b>TIMEOUT</b>\n{symbole} | {'+' if gain_total>=0 else ''}{gain_total}€\nDurée : {duree} min")
            return resultat, gain_total, mise, trade_info

def verifier_kill_switch(etat, capital):
    if capital < CAPITAL_INITIAL * SEUIL_RUINE:
        log.critical(f"SEUIL DE RUINE ! Capital {capital}EUR")
        telegram(f"🚨 <b>SEUIL DE RUINE !</b>\nCapital : {capital}€\nBot arrêté !")
        return "RUINE"
    pause_until = etat.get("pause_until", 0)
    if time.time() < pause_until:
        restant = int((pause_until - time.time()) / 60)
        log.info(f"  En pause — {restant} minutes restantes")
        time.sleep(60)
        return "PAUSE"
    else:
        if etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:
            log.info("  Fin de la pause → réinitialisation des pertes consécutives à 0")
            etat["pertes_consecutives"] = 0
            sauvegarder_etat(etat)
    if etat["pertes_consecutives"] >= MAX_PERTES_CONSECUTIVES:
        log.warning(f"KILL SWITCH — {MAX_PERTES_CONSECUTIVES} pertes consecutives !")
        telegram(f"⚠️ <b>KILL SWITCH</b>\n{MAX_PERTES_CONSECUTIVES} pertes consécutives\nPause 12h")
        etat["pause_until"]         = int(time.time()) + PAUSE_DUREE
        etat["pertes_consecutives"] = 0
        sauvegarder_etat(etat)
        return "PAUSE"
    return "OK"

def afficher_tableau_de_bord(etat):
    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
    perf     = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    log.info(f"\n  {'='*55}")
    log.info(f"  BOT MEAN REVERSION V8 — TABLEAU DE BORD")
    log.info(f"  {'='*55}")
    log.info(f"  Capital actuel : {round(etat['capital'],2)}EUR ({'+' if perf >= 0 else ''}{round(perf,2)}%)")
    log.info(f"  Trades total   : {etat['nb_trades']}")
    log.info(f"  Victoires      : {etat['nb_wins']} ({win_rate:.1f}%)")
    log.info(f"  Defaites       : {etat['nb_losses']}")
    log.info(f"  Pertes consec. : {etat['pertes_consecutives']}/{MAX_PERTES_CONSECUTIVES}")
    log.info(f"  Kelly actif    : {'Non (<30 trades)' if etat['nb_trades'] < MIN_TRADES_KELLY else 'Oui'}")
    log.info(f"  Total gagne    : +{round(etat['total_gagne'],2)}EUR")
    log.info(f"  Total perdu    : -{round(etat['total_perdu'],2)}EUR")
    log.info(f"  BENEFICE NET   : {'+' if etat['cumul_net'] >= 0 else ''}{round(etat['cumul_net'],2)}EUR")
    log.info(f"  [V8] Régime    : {regime_actuel['mode']} | "
             f"Mise {regime_actuel['mise_pct']*100}% | "
             f"RSI {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}")
    log.info(f"  Trailing Stop  : protection active dès +0.75€")
    if etat.get("historique"):
        log.info(f"\n  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "OK" if h["resultat"] == "GAGNE" else "XX"
            log.info(f"    [{icone}] {h['heure']} | {h['marche']} | {h['direction']} | {'+' if h['gain'] >= 0 else ''}{h['gain']}EUR | Capital: {h['capital']}EUR")
    log.info(f"  {'='*55}")

def envoyer_rapport_telegram(etat):
    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
    perf     = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    telegram(f"📈 <b>RAPPORT BOT V8</b>\n"
             f"Capital : <b>{round(etat['capital'],2)}€</b> ({'+' if perf>=0 else ''}{round(perf,2)}%)\n"
             f"Trades : {etat['nb_trades']} | WR : {round(win_rate,1)}%\n"
             f"Gagné : +{round(etat['total_gagne'],2)}€\n"
             f"Perdu : -{round(etat['total_perdu'],2)}€\n"
             f"<b>NET : {'+' if etat['cumul_net']>=0 else ''}{round(etat['cumul_net'],2)}€</b>\n"
             f"Mode V8 : {regime_actuel['mode']}\n"
             f"Protection trailing stop active dès +0.75€")

def demarrer_bot():
    log.info("=" * 55)
    log.info("  BOT MEAN REVERSION V8 — ADAPTATIF")
    log.info(f"  Capital : {CAPITAL_INITIAL}EUR | Levier x{LEVIER}")
    log.info(f"  Base V7.3 + Détection régime automatique")
    log.info(f"  Scan régime toutes les {V8_SCAN_INTERVAL//60} minutes")
    log.info(f"  Trailing Stop : PnL + Multiplicateur + Protège")
    log.info(f"  Paliers : protection DÈS +0.75€ !")
    log.info(f"  Telegram : {'✅ ON' if TELEGRAM_TOKEN else '❌ OFF'}")
    log.info("=" * 55)

    init_database()
    etat = charger_etat()

    log.info("  [V8] Analyse initiale du régime de marché...")
    detecter_regime()

    afficher_tableau_de_bord(etat)

    telegram(f"🚀 <b>BOT V8 DÉMARRÉ</b>\n"
             f"Capital : {round(etat['capital'],2)}€\n"
             f"Régime initial : {regime_actuel['mode']}\n"
             f"Mise : {regime_actuel['mise_pct']*100}% | "
             f"RSI : {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}\n"
             f"Trailing stop : protection active dès +0.75€\n"
             f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        try:
            if time.time() - regime_actuel["derniere_maj"] >= V8_SCAN_INTERVAL:
                log.info("  [V8] Mise à jour du régime de marché...")
                detecter_regime()

            statut = verifier_kill_switch(etat, etat["capital"])
            if statut == "RUINE":
                break
            if statut == "PAUSE":
                etat = charger_etat()
                continue

            symbole, direction, details = choisir_meilleur_marche()

            if direction == "NEUTRE" or symbole is None:
                etat["nb_skips"] += 1
                sauvegarder_etat(etat)
                log.info(f"  Nouvelle analyse dans 2 minutes...")
                time.sleep(PAUSE)
                continue

            etat["nb_trades"] += 1
            resultat, gain, mise, trade_info = simuler_trade(
                symbole, direction, etat["nb_trades"],
                etat["capital"], details, etat
            )

            if resultat == "ERREUR":
                etat["nb_trades"] -= 1
                time.sleep(PAUSE)
                continue

            etat["capital"]   = round(etat["capital"] + gain, 2)
            etat["cumul_net"] = round(etat["capital"] - CAPITAL_INITIAL, 2)

            if resultat == "GAGNE":
                etat["nb_wins"]            += 1
                etat["total_gagne"]         = round(etat["total_gagne"] + gain, 2)
                etat["pertes_consecutives"] = 0
                gain_pct = (gain / max(mise * LEVIER, 1)) * 100
                if etat["avg_win_pct"] == 0:
                    etat["avg_win_pct"] = gain_pct
                else:
                    etat["avg_win_pct"] = round((etat["avg_win_pct"] * (etat["nb_wins"]-1) + gain_pct) / etat["nb_wins"], 4)
            else:
                etat["nb_losses"]          += 1
                etat["total_perdu"]         = round(etat["total_perdu"] + abs(gain), 2)
                etat["pertes_consecutives"] += 1
                perte_pct = (abs(gain) / max(mise * LEVIER, 1)) * 100
                if etat["avg_loss_pct"] == 0:
                    etat["avg_loss_pct"] = perte_pct
                else:
                    etat["avg_loss_pct"] = round((etat["avg_loss_pct"] * (etat["nb_losses"]-1) + perte_pct) / etat["nb_losses"], 4)

            enregistrer_trade({
                'marche':        symbole,
                'direction':     direction,
                'resultat':      resultat,
                'prix_entree':   trade_info['prix_entree'],
                'prix_sortie':   trade_info['prix_sortie'],
                'stop_loss':     trade_info['stop_loss'],
                'objectif':      trade_info['objectif'],
                'mise':          mise,
                'gain':          round(gain, 2),
                'capital_apres': etat['capital'],
                'duree_minutes': trade_info['duree_minutes'],
                'score':         None,
                'adx':           details.get('adx'),
                'atr':           details.get('atr'),
                'rsi':           details.get('rsi'),
            })

            sauvegarder_etat(etat)
            etat['historique'].append({
                'heure':     datetime.now().strftime('%Y-%m-%d %H:%M'),
                'marche':    symbole,
                'direction': direction,
                'resultat':  resultat,
                'gain':      round(gain, 2),
                'mise':      round(mise, 2),
                'capital':   etat['capital']
            })

            afficher_tableau_de_bord(etat)
            envoyer_rapport_telegram(etat)
            log.info(f"  Pause 2 minutes avant prochain trade...")
            time.sleep(PAUSE)

        except KeyboardInterrupt:
            log.info("Bot arrete.")
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            time.sleep(60)

if __name__ == "__main__":
    demarrer_bot()
