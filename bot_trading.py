"""
╔══════════════════════════════════════════════════════════════════╗
║         BOT HUMAIN — VÉRONIQUE973 V2                            ║
║  Multi-timeframe | Range vs Tendance | Heures intelligentes     ║
║  Lock profits paliers | Mise dynamique | 3 trades simultanés   ║
║  Capital 500€ | Architecture async aiohttp                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import aiohttp
import os
import logging
import json
import re
import time
from datetime import datetime, timezone
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from database import init_database, charger_etat, sauvegarder_etat, enregistrer_trade

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
CAPITAL_INITIAL         = 500.0
LEVIER                  = 10
MISE_BASE_PCT           = 0.10       # 10% du capital = 50€ de base
MISE_MIN                = 10.0
MISE_MAX_PCT            = 0.25
ATR_MULTIPLIER_STOP     = 2.5
ATR_MULTIPLIER_TARGET   = 5.0
CHECK_INTERVAL          = 10
PAUSE_ENTRE_TRADES      = 120
TIMEOUT_TRADE           = 8 * 3600
MAX_TRADES_SIMULTANES   = 3

# Filtres signaux
RSI_OVERSOLD            = 30
RSI_OVERBOUGHT          = 70
ADX_RANGE_MAX           = 22
ADX_TREND_MIN           = 25
VOLUME_MINI             = 0.30

# Gestion mise dynamique
WINS_CONFIANCE          = 3
BOOST_CONFIANCE         = 1.20
REDUCTION_PERTES        = 0.50
MIN_TRADES_KELLY        = 30
KELLY_FRACTION          = 0.25
KELLY_CAP               = 0.20

# Protections
KILL_SWITCH_JOUR        = -25.0
SEUIL_RUINE             = 300.0
MAX_PERTES_CONSECUTIVES = 2
COOLDOWN_PERTES         = 1800

# Lock profits paliers
LOCK_PALIERS = [0.75, 1.50, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0]

# Heures de trading UTC
HEURES_LONDON    = (7, 12)
HEURES_NEWYORK   = (13, 17)
HEURES_NUIT      = (23, 6)

# Position dans le range
RANGE_BAS_SEUIL  = 0.20
RANGE_HAUT_SEUIL = 0.80
RANGE_HEURES     = 8

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

MARCHES = [
    "ETHUSDT", "XRPUSDT", "SOLUSDT",  "ADAUSDT",
    "LINKUSDT", "ATOMUSDT", "AVAXUSDT", "NEARUSDT",
    "DOTUSDT", "DOGEUSDT", "LTCUSDT",  "ALGOUSDT", "TRXUSDT"
]

KRAKEN_SYMBOLS = {
    "ETHUSDT":  "XETHZUSD",
    "XRPUSDT":  "XXRPZUSD",
    "SOLUSDT":  "SOLUSD",
    "ADAUSDT":  "ADAUSD",
    "LINKUSDT": "LINKUSD",
    "ATOMUSDT": "ATOMUSD",
    "AVAXUSDT": "AVAXUSD",
    "NEARUSDT": "NEARUSD",
    "DOTUSDT":  "DOTUSD",
    "DOGEUSDT": "XDGUSD",
    "LTCUSDT":  "XLTCZUSD",
    "ALGOUSDT": "ALGOUSD",
    "TRXUSDT":  "TRXUSD",
}

# ═══════════════════════════════════════════════════════════════
#  ÉTAT GLOBAL DES TRADES OUVERTS
# ═══════════════════════════════════════════════════════════════
trades_ouverts = {}   # { symbole: True } — marchés actuellement en trade
trades_lock    = None  # initialisé dans boucle_principale()

log.info("=" * 60)
log.info("  BOT HUMAIN — VÉRONIQUE973 V2")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER}")
log.info(f"  Marchés : {len(MARCHES)} cryptos | Max {MAX_TRADES_SIMULTANES} trades simultanés")
log.info(f"  Multi-timeframe : 2h → 1h → 15min")
log.info(f"  Range vs Tendance | Heures intelligentes")
log.info(f"  Lock paliers : {LOCK_PALIERS}")
log.info(f"  Kill switch : {KILL_SWITCH_JOUR}€/jour | Ruine : {SEUIL_RUINE}€")
log.info(f"  Telegram : {'ON' if TELEGRAM_TOKEN else 'OFF'}")
log.info("=" * 60)

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════
async def telegram(session, message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")

# ═══════════════════════════════════════════════════════════════
#  DONNÉES MARCHÉ
# ═══════════════════════════════════════════════════════════════
async def get_klines(session, symbole, interval=60, limite=100):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/OHLC"
    try:
        async with session.get(url, params={"pair": kraken_symbol, "interval": interval},
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
            if data.get("error"):
                return None
            result = data.get("result", {})
            keys = [k for k in result.keys() if k != "last"]
            if not keys:
                return None
            candles = result[keys[0]]
            df = pd.DataFrame(candles, columns=[
                'time','open','high','low','close','vwap','volume','count'
            ])
            df = df.astype({
                'open': float, 'high': float, 'low': float,
                'close': float, 'volume': float
            })
            return df.tail(limite).reset_index(drop=True)
    except Exception as e:
        log.error(f"Erreur klines {symbole} ({interval}min) : {e}")
        return None

async def get_prix_actuel(session, symbole):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    try:
        async with session.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": kraken_symbol},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
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

# ═══════════════════════════════════════════════════════════════
#  INDICATEURS TECHNIQUES
# ═══════════════════════════════════════════════════════════════
def calc_rsi(df, periode=14):
    try:
        val = RSIIndicator(close=df['close'], window=periode).rsi().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 50.0
    except:
        return 50.0

def calc_adx(df, periode=14):
    try:
        val = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=periode).adx().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 0.0
    except:
        return 0.0

def calc_atr(df, periode=14):
    try:
        val = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=periode).average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0.0
    except:
        return 0.0

def calc_ema(df, periode=20):
    try:
        val = EMAIndicator(close=df['close'], window=periode).ema_indicator().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0.0
    except:
        return 0.0

def calc_volume_ratio(df):
    try:
        volumes = df['volume'].tolist()
        if len(volumes) < 10:
            return 0.0
        moyenne = sum(volumes[-25:-1]) / 24
        recent  = volumes[-2]
        return round(recent / moyenne, 2) if moyenne > 0 else 0.0
    except:
        return 0.0

# ═══════════════════════════════════════════════════════════════
#  HEURES DE TRADING
# ═══════════════════════════════════════════════════════════════
def get_session_actuelle():
    heure_utc = datetime.now(timezone.utc).hour
    h_debut_nuit, h_fin_nuit = HEURES_NUIT
    if heure_utc >= h_debut_nuit or heure_utc < h_fin_nuit:
        return "nuit", None
    if HEURES_LONDON[0] <= heure_utc < HEURES_LONDON[1]:
        return "london", 1.0
    if HEURES_NEWYORK[0] <= heure_utc < HEURES_NEWYORK[1]:
        return "newyork", 1.0
    return "asie", 0.5

# ═══════════════════════════════════════════════════════════════
#  POSITION DANS LE RANGE
# ═══════════════════════════════════════════════════════════════
def analyser_position_range(df_1h):
    try:
        df_range     = df_1h.tail(RANGE_HEURES)
        haut_session = df_range['high'].max()
        bas_session  = df_range['low'].min()
        prix_actuel  = float(df_1h['close'].iloc[-1])
        range_total  = haut_session - bas_session
        if range_total <= 0:
            return 0.5, haut_session, bas_session, prix_actuel
        position = (prix_actuel - bas_session) / range_total
        return round(position, 3), round(haut_session, 6), round(bas_session, 6), round(prix_actuel, 6)
    except:
        return 0.5, 0, 0, 0

# ═══════════════════════════════════════════════════════════════
#  DÉTECTION RÉGIME
# ═══════════════════════════════════════════════════════════════
def detecter_regime(df_2h, df_1h):
    adx_2h = calc_adx(df_2h)
    adx_1h = calc_adx(df_1h)
    if adx_2h < ADX_RANGE_MAX and adx_1h < ADX_RANGE_MAX + 5:
        return "RANGE", adx_2h, adx_1h
    elif adx_2h >= ADX_TREND_MIN or adx_1h >= ADX_TREND_MIN:
        return "TENDANCE", adx_2h, adx_1h
    else:
        return "NEUTRE", adx_2h, adx_1h

# ═══════════════════════════════════════════════════════════════
#  MOMENTUM 15MIN — direction du RSI sur les 3 dernières bougies
# ═══════════════════════════════════════════════════════════════
def calc_momentum_15m(df_15m):
    """
    Retourne True si le RSI 15min monte (momentum haussier)
    Retourne False si le RSI 15min descend (momentum baissier)
    Compare la moyenne des 3 dernières bougies vs les 3 précédentes.
    """
    try:
        rsi_series = RSIIndicator(close=df_15m['close'], window=14).rsi().dropna().tolist()
        if len(rsi_series) < 6:
            return None  # pas assez de données
        recent   = sum(rsi_series[-3:]) / 3   # moyenne 3 dernières bougies
        precedent = sum(rsi_series[-6:-3]) / 3  # moyenne 3 bougies avant
        if recent > precedent + 1.0:
            return True   # momentum haussier clair
        elif recent < precedent - 1.0:
            return False  # momentum baissier clair
        else:
            return None   # neutre, pas assez directionnel
    except:
        return None

# ═══════════════════════════════════════════════════════════════
#  ANALYSE MULTI-TIMEFRAME — VERSION RENFORCÉE
#  Règle d'or : entrer seulement quand les 3 TF s'alignent
#  ET que le momentum 15min confirme la direction
# ═══════════════════════════════════════════════════════════════
async def analyser_multi_timeframe(session, symbole):
    df_2h  = await get_klines(session, symbole, interval=120, limite=50)
    df_1h  = await get_klines(session, symbole, interval=60,  limite=100)
    df_15m = await get_klines(session, symbole, interval=15,  limite=100)

    if df_2h is None or df_1h is None or df_15m is None:
        return "NEUTRE", 0, {}
    if len(df_2h) < 20 or len(df_1h) < 30 or len(df_15m) < 30:
        return "NEUTRE", 0, {}

    regime, adx_2h, adx_1h = detecter_regime(df_2h, df_1h)

    # ── Indicateurs 2h (juge de la tendance principale)
    rsi_2h   = calc_rsi(df_2h)
    ema20_2h = calc_ema(df_2h, 20)
    ema50_2h = calc_ema(df_2h, 50)
    prix_2h  = float(df_2h['close'].iloc[-1])

    # ── Indicateurs 1h
    rsi_1h   = calc_rsi(df_1h)
    ema20_1h = calc_ema(df_1h, 20)
    ema50_1h = calc_ema(df_1h, 50)
    prix_1h  = float(df_1h['close'].iloc[-1])

    # ── Indicateurs 15min
    rsi_15m  = calc_rsi(df_15m)
    momentum_haussier = calc_momentum_15m(df_15m)  # True/False/None

    atr_1h    = calc_atr(df_1h)
    vol_ratio = calc_volume_ratio(df_1h)
    position_range, haut, bas, prix_actuel = analyser_position_range(df_1h)

    details = {
        "regime": regime, "adx_2h": adx_2h, "adx_1h": adx_1h,
        "rsi_2h": rsi_2h, "rsi_1h": rsi_1h, "rsi_15m": rsi_15m,
        "ema20": ema20_1h, "ema50": ema50_1h, "prix": prix_actuel,
        "atr": atr_1h, "vol_ratio": vol_ratio,
        "position_range": position_range,
        "haut_session": haut, "bas_session": bas,
        "momentum": momentum_haussier,
    }

    # ── Filtre volume
    if vol_ratio < VOLUME_MINI:
        log.info(f"  {symbole} : Volume {vol_ratio:.2f}x → skip")
        return "NEUTRE", 0, details

    # ════════════════════════════════════════════════════════
    #  STRATÉGIE RANGE
    # ════════════════════════════════════════════════════════
    if regime == "RANGE":
        score = 0
        direction = "NEUTRE"

        if position_range <= RANGE_BAS_SEUIL:
            direction = "ACHAT"
            # Filtre bloquant : seulement si momentum CLAIREMENT baissier
            if momentum_haussier is False:
                log.info(f"  {symbole} [RANGE] Bas mais momentum clairement baissier → skip")
                return "NEUTRE", 0, details
            # Filtre bloquant : 2h ne doit pas être en tendance baissière forte
            if prix_2h < ema20_2h and rsi_2h < 40:
                log.info(f"  {symbole} [RANGE] 2h trop baissier (prix<EMA20 RSI={rsi_2h}) → skip")
                return "NEUTRE", 0, details
            score += 3  # position au bas du range
            if rsi_1h < RSI_OVERSOLD:          score += 2  # RSI survendu 1h
            if rsi_2h < 50:                    score += 1  # 2h pas suracheté
            if rsi_15m < RSI_OVERSOLD + 5:     score += 1  # 15min aussi bas
            if momentum_haussier is True:       score += 1  # momentum haussier (bonus)
            if vol_ratio >= 0.50:               score += 1  # volume correct
            mom_str = '↑' if momentum_haussier is True else ('?' if momentum_haussier is None else '↓')
            log.info(f"  {symbole} [RANGE↑] Bas {position_range:.0%} | RSI 2h={rsi_2h} 1h={rsi_1h} 15m={rsi_15m} | Mom={mom_str} | Score={score}/9")

        elif position_range >= RANGE_HAUT_SEUIL:
            direction = "VENTE"
            # Filtre bloquant : seulement si momentum CLAIREMENT haussier
            if momentum_haussier is True:
                log.info(f"  {symbole} [RANGE] Haut mais momentum clairement haussier → skip")
                return "NEUTRE", 0, details
            # Filtre bloquant : 2h ne doit pas être en tendance haussière forte
            if prix_2h > ema20_2h and rsi_2h > 60:
                log.info(f"  {symbole} [RANGE] 2h trop haussier (prix>EMA20 RSI={rsi_2h}) → skip")
                return "NEUTRE", 0, details
            score += 3  # position au haut du range
            if rsi_1h > RSI_OVERBOUGHT:        score += 2  # RSI suracheté 1h
            if rsi_2h > 50:                    score += 1  # 2h pas survendu
            if rsi_15m > RSI_OVERBOUGHT - 5:   score += 1  # 15min aussi haut
            if momentum_haussier is False:      score += 1  # momentum baissier (bonus)
            if vol_ratio >= 0.50:               score += 1  # volume correct
            mom_str = '↓' if momentum_haussier is False else ('?' if momentum_haussier is None else '↑')
            log.info(f"  {symbole} [RANGE↓] Haut {position_range:.0%} | RSI 2h={rsi_2h} 1h={rsi_1h} 15m={rsi_15m} | Mom={mom_str} | Score={score}/9")

        else:
            log.info(f"  {symbole} [RANGE] Neutre {position_range:.0%} → skip")
            return "NEUTRE", 0, details

        # Score minimum 5/9
        if score < 5:
            log.info(f"  {symbole} [RANGE] Score {score}/9 insuffisant → skip")
            return "NEUTRE", 0, details
        return direction, score, details

    # ════════════════════════════════════════════════════════
    #  STRATÉGIE TENDANCE
    #  Règle : les 3 TF (2h + 1h + 15min) doivent confirmer
    # ════════════════════════════════════════════════════════
    elif regime == "TENDANCE":
        score = 0
        direction = "NEUTRE"

        # ── ACHAT : tendance haussière confirmée sur 3 TF
        if prix_1h > ema20_1h > ema50_1h:
            direction = "ACHAT"

            # Filtre bloquant 2h — OBLIGATOIRE
            if not (prix_2h > ema20_2h and rsi_2h > 50):
                log.info(f"  {symbole} [TENDANCE↑] 2h pas confirmé (prix={prix_2h:.4f} EMA20={ema20_2h:.4f} RSI={rsi_2h}) → skip")
                return "NEUTRE", 0, details

            # Filtre bloquant momentum 15min — seulement si CLAIREMENT baissier
            if momentum_haussier is False:
                log.info(f"  {symbole} [TENDANCE↑] Momentum 15min clairement baissier → skip")
                return "NEUTRE", 0, details

            score += 3  # 1h aligné (prix > EMA20 > EMA50)
            score += 2  # 2h confirmé (prix > EMA20 2h et RSI > 50)
            if rsi_1h > 50 and rsi_1h < RSI_OVERBOUGHT: score += 1  # RSI 1h sain
            if rsi_2h > 55:                              score += 1  # 2h fort
            if momentum_haussier is True:                score += 1  # momentum clair
            if vol_ratio >= 0.50:                        score += 1  # volume
            log.info(f"  {symbole} [TENDANCE↑] RSI 2h={rsi_2h} 1h={rsi_1h} 15m={rsi_15m} | Mom=↑ | Score={score}/9")

        # ── VENTE : tendance baissière confirmée sur 3 TF
        elif prix_1h < ema20_1h < ema50_1h:
            direction = "VENTE"

            # Filtre bloquant 2h — OBLIGATOIRE
            if not (prix_2h < ema20_2h and rsi_2h < 50):
                log.info(f"  {symbole} [TENDANCE↓] 2h pas confirmé (prix={prix_2h:.4f} EMA20={ema20_2h:.4f} RSI={rsi_2h}) → skip")
                return "NEUTRE", 0, details

            # Filtre bloquant momentum 15min — seulement si CLAIREMENT haussier
            if momentum_haussier is True:
                log.info(f"  {symbole} [TENDANCE↓] Momentum 15min clairement haussier → skip")
                return "NEUTRE", 0, details

            score += 3  # 1h aligné (prix < EMA20 < EMA50)
            score += 2  # 2h confirmé (prix < EMA20 2h et RSI < 50)
            if rsi_1h < 50 and rsi_1h > RSI_OVERSOLD:   score += 1  # RSI 1h sain
            if rsi_2h < 45:                              score += 1  # 2h fort baissier
            if momentum_haussier is False:               score += 1  # momentum clair
            if vol_ratio >= 0.50:                        score += 1  # volume
            log.info(f"  {symbole} [TENDANCE↓] RSI 2h={rsi_2h} 1h={rsi_1h} 15m={rsi_15m} | Mom=↓ | Score={score}/9")

        else:
            log.info(f"  {symbole} [TENDANCE] EMAs croisées → skip")
            return "NEUTRE", 0, details

        # Score minimum 5/9
        if score < 5:
            log.info(f"  {symbole} [TENDANCE] Score {score}/9 insuffisant → skip")
            return "NEUTRE", 0, details
        return direction, score, details

    else:
        log.info(f"  {symbole} [NEUTRE] ADX 2h={adx_2h} 1h={adx_1h} → skip")
        return "NEUTRE", 0, details

# ═══════════════════════════════════════════════════════════════
#  GESTION MISE DYNAMIQUE
# ═══════════════════════════════════════════════════════════════
def calculer_mise(capital, etat, multiplicateur_session):
    nb_trades     = etat.get("nb_trades", 0)
    nb_wins       = etat.get("nb_wins", 0)
    wins_consec   = etat.get("wins_consecutifs", 0)
    pertes_consec = etat.get("pertes_consecutives", 0)
    avg_win_pct   = etat.get("avg_win_pct", 0)
    avg_loss_pct  = etat.get("avg_loss_pct", 0)

    mise = capital * MISE_BASE_PCT

    if nb_trades >= MIN_TRADES_KELLY and avg_loss_pct > 0 and avg_win_pct > 0:
        win_rate   = nb_wins / nb_trades
        b          = avg_win_pct / avg_loss_pct
        kelly_full = (win_rate * b - (1 - win_rate)) / b
        kelly_frac = max(0, min(kelly_full * KELLY_FRACTION, KELLY_CAP))
        mise       = capital * kelly_frac

    if pertes_consec >= 2:
        mise *= REDUCTION_PERTES
        log.info(f"  ⚠️ Mise réduite 50% ({pertes_consec} pertes)")
    elif wins_consec >= WINS_CONFIANCE:
        mise *= BOOST_CONFIANCE
        log.info(f"  💪 Mise boostée +20% ({wins_consec} wins)")

    mise *= (multiplicateur_session or 1.0)
    mise  = max(mise, MISE_MIN)
    mise  = min(mise, capital * MISE_MAX_PCT)
    return round(mise, 2)

# ═══════════════════════════════════════════════════════════════
#  LOCK PROFITS
# ═══════════════════════════════════════════════════════════════
def get_palier_lock(pnl_max):
    lock = 0.0
    for palier in LOCK_PALIERS:
        if pnl_max >= palier:
            lock = palier
    return lock

# ═══════════════════════════════════════════════════════════════
#  EXÉCUTION D'UN TRADE (coroutine indépendante)
# ═══════════════════════════════════════════════════════════════
async def executer_trade(session, symbole, direction, capital, details, etat, multiplicateur_session, etat_global):
    prix_entree = await get_prix_actuel(session, symbole)
    if prix_entree is None:
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        return "ERREUR", 0, 0, {}

    atr  = details.get("atr", 0)
    mise = calculer_mise(capital, etat, multiplicateur_session)

    if direction == "ACHAT":
        stop_loss      = round(prix_entree - atr * ATR_MULTIPLIER_STOP, 8)
        objectif_final = round(prix_entree + atr * ATR_MULTIPLIER_TARGET, 8)
    else:
        stop_loss      = round(prix_entree + atr * ATR_MULTIPLIER_STOP, 8)
        objectif_final = round(prix_entree - atr * ATR_MULTIPLIER_TARGET, 8)

    distance_stop_pct = abs(prix_entree - stop_loss) / prix_entree * 100
    regime  = details.get("regime", "?")
    session_nom, _ = get_session_actuelle()

    # Numéro de trade calculé depuis l'état
    async with trades_lock:
        etat_global["nb_trades"] = etat_global.get("nb_trades", 0) + 1
        numero_trade = etat_global["nb_trades"]

    log.info(f"\n  {'='*55}")
    log.info(f"  TRADE #{numero_trade} [VÉRONIQUE973] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  Symbole  : {symbole} ({direction}) | Régime : {regime}")
    log.info(f"  RSI      : 2h={details.get('rsi_2h')} | 1h={details.get('rsi_1h')} | 15m={details.get('rsi_15m')}")
    log.info(f"  Range    : {details.get('position_range', 0):.0%}")
    log.info(f"  Prix     : {prix_entree} | Stop : {stop_loss} | Obj : {objectif_final}")
    log.info(f"  Mise     : {mise}€ × x{LEVIER} = {round(mise*LEVIER,2)}€")
    log.info(f"  Trades ouverts : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n")

    await telegram(session, f"📊 <b>TRADE #{numero_trade} — VÉRONIQUE973</b>\n"
                   f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
                   f"Régime : {regime} | Session : {session_nom.upper()}\n"
                   f"RSI 2h={details.get('rsi_2h')} 1h={details.get('rsi_1h')}\n"
                   f"Prix : {prix_entree} | Stop : {stop_loss}\n"
                   f"Objectif : {objectif_final}\n"
                   f"Mise : {mise}€ × x{LEVIER}\n"
                   f"Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES} 🔒 Lock actifs")

    debut           = time.time()
    dernier_log     = 0
    prix_sortie     = prix_entree
    pnl_max_atteint = 0.0
    lock_actuel     = 0.0
    resultat_final  = None
    gain_final      = 0.0

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        prix_actuel = await get_prix_actuel(session, symbole)
        if prix_actuel is None:
            continue

        prix_sortie = prix_actuel

        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        nouveau_lock = get_palier_lock(pnl_max_atteint)
        if nouveau_lock > lock_actuel:
            lock_actuel = nouveau_lock
            log.info(f"  🔒 LOCK {lock_actuel}€ garanti ! [{symbole}] (max={pnl_max_atteint:.2f}€)")
            await telegram(session, f"🔒 <b>Lock {lock_actuel}€ garanti !</b>\n{symbole} | PnL={pnl:.2f}€")

        # Sortie lock
        if lock_actuel > 0 and pnl < lock_actuel and pnl_max_atteint > lock_actuel:
            duree = int((time.time() - debut) / 60)
            log.info(f"\n  🔒 SORTIE LOCK [{symbole}] — +{lock_actuel}€")
            await telegram(session, f"🔒 <b>SORTIE LOCK</b>\n{symbole}\nGain : <b>+{lock_actuel}€</b>\nDurée : {duree} min")
            resultat_final = "GAGNE"
            gain_final     = lock_actuel
            break

        atteint_final = (prix_actuel >= objectif_final if direction == "ACHAT" else prix_actuel <= objectif_final)
        atteint_stop  = (prix_actuel <= stop_loss if direction == "ACHAT" else prix_actuel >= stop_loss)
        duree = int((time.time() - debut) / 60)

        if time.time() - dernier_log >= 60:
            lock_flag = f" 🔒{lock_actuel}€" if lock_actuel > 0 else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl}€{lock_flag} | {duree}min")
            dernier_log = time.time()

        if atteint_final:
            log.info(f"\n  🎯 OBJECTIF ! [{symbole}] +{pnl}€")
            await telegram(session, f"🎯 <b>OBJECTIF !</b>\n{symbole} +{pnl}€\nDurée : {duree} min")
            resultat_final = "GAGNE"
            gain_final     = pnl
            break

        if atteint_stop:
            resultat_final = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  🛑 STOP [{symbole}] {pnl}€")
            await telegram(session, f"🛑 <b>STOP</b>\n{symbole} {pnl}€\nDurée : {duree} min")
            gain_final = pnl
            break

        if time.time() - debut >= TIMEOUT_TRADE:
            resultat_final = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  ⏱ TIMEOUT [{symbole}] {'+' if pnl>=0 else ''}{pnl}€")
            await telegram(session, f"⏱ <b>TIMEOUT</b>\n{symbole} {'+' if pnl>=0 else ''}{pnl}€\nDurée : {duree} min")
            gain_final = pnl
            break

    # Libérer le marché
    async with trades_lock:
        trades_ouverts.pop(symbole, None)

    trade_info = {
        "prix_entree": prix_entree, "prix_sortie": prix_sortie,
        "stop_loss": stop_loss, "objectif": objectif_final,
        "duree_minutes": int((time.time() - debut) / 60)
    }

    # Mettre à jour l'état global (protégé)
    async with trades_lock:
        etat_global["capital"]   = round(etat_global["capital"] + gain_final, 2)
        etat_global["cumul_net"] = round(etat_global["capital"] - CAPITAL_INITIAL, 2)
        etat_global["pnl_jour"]  = round(etat_global.get("pnl_jour", 0) + gain_final, 2)

        if resultat_final == "GAGNE":
            etat_global["nb_wins"]             = etat_global.get("nb_wins", 0) + 1
            etat_global["total_gagne"]         = round(etat_global.get("total_gagne", 0) + gain_final, 2)
            etat_global["pertes_consecutives"] = 0
            etat_global["wins_consecutifs"]    = etat_global.get("wins_consecutifs", 0) + 1
            n = etat_global["nb_wins"]
            gain_pct = (gain_final / max(mise * LEVIER, 1)) * 100
            etat_global["avg_win_pct"] = round(
                (etat_global.get("avg_win_pct", 0) * (n-1) + gain_pct) / n, 4
            )
        else:
            etat_global["nb_losses"]           = etat_global.get("nb_losses", 0) + 1
            etat_global["total_perdu"]         = round(etat_global.get("total_perdu", 0) + abs(gain_final), 2)
            etat_global["pertes_consecutives"] = etat_global.get("pertes_consecutives", 0) + 1
            etat_global["wins_consecutifs"]    = 0
            n = etat_global["nb_losses"]
            perte_pct = (abs(gain_final) / max(mise * LEVIER, 1)) * 100
            etat_global["avg_loss_pct"] = round(
                (etat_global.get("avg_loss_pct", 0) * (n-1) + perte_pct) / n, 4
            )

        etat_global.setdefault("historique", []).append({
            'heure':     datetime.now().strftime('%Y-%m-%d %H:%M'),
            'marche':    symbole,
            'direction': direction,
            'resultat':  resultat_final,
            'gain':      round(gain_final, 2),
            'mise':      round(mise, 2),
            'capital':   etat_global["capital"]
        })

    enregistrer_trade({
        'marche': symbole, 'direction': direction, 'resultat': resultat_final,
        'prix_entree': trade_info['prix_entree'], 'prix_sortie': trade_info['prix_sortie'],
        'stop_loss': trade_info['stop_loss'], 'objectif': trade_info['objectif'],
        'mise': mise, 'gain': round(gain_final, 2),
        'capital_apres': etat_global['capital'],
        'duree_minutes': trade_info['duree_minutes'],
        'score': None, 'adx': details.get('adx_1h'),
        'atr': details.get('atr'), 'rsi': details.get('rsi_1h'),
    })
    sauvegarder_etat(etat_global)
    afficher_tableau_de_bord(etat_global)

    return resultat_final, gain_final, mise, trade_info

# ═══════════════════════════════════════════════════════════════
#  PROTECTIONS
# ═══════════════════════════════════════════════════════════════
def verifier_protections(etat, capital):
    if capital < SEUIL_RUINE:
        log.critical(f"🚨 SEUIL RUINE ! Capital {capital}€ → ARRÊT")
        return "RUINE"
    pnl_jour = etat.get("pnl_jour", 0.0)
    if pnl_jour <= KILL_SWITCH_JOUR:
        log.warning(f"⚠️ KILL SWITCH — PnL jour {pnl_jour}€")
        return "KILL_SWITCH"
    cooldown_until = etat.get("cooldown_until", 0)
    if time.time() < cooldown_until:
        restant = int((cooldown_until - time.time()) / 60)
        log.info(f"  ❄️ Cooldown — {restant} min restantes")
        return "COOLDOWN"
    if cooldown_until > 0 and time.time() >= cooldown_until:
        etat["pertes_consecutives"] = 0
        etat["cooldown_until"]      = 0
        sauvegarder_etat(etat)
    if etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:
        log.warning(f"  {MAX_PERTES_CONSECUTIVES} pertes → cooldown {COOLDOWN_PERTES//60} min")
        etat["cooldown_until"]      = int(time.time()) + COOLDOWN_PERTES
        etat["pertes_consecutives"] = 0
        sauvegarder_etat(etat)
        return "COOLDOWN"
    return "OK"

def reset_pnl_jour_si_nouveau_jour(etat):
    aujourd_hui = datetime.now().strftime('%Y-%m-%d')
    if etat.get("date_jour", "") != aujourd_hui:
        etat["pnl_jour"]  = 0.0
        etat["date_jour"] = aujourd_hui
        log.info(f"  📅 Nouveau jour — PnL remis à 0")

# ═══════════════════════════════════════════════════════════════
#  TABLEAU DE BORD
# ═══════════════════════════════════════════════════════════════
def afficher_tableau_de_bord(etat):
    nb_trades = etat.get("nb_trades", 0)
    nb_wins   = etat.get("nb_wins", 0)
    win_rate  = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0
    perf      = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    log.info(f"\n  {'='*55}")
    log.info(f"  BOT HUMAIN — VÉRONIQUE973 V2")
    log.info(f"  {'='*55}")
    log.info(f"  Capital    : {round(etat['capital'],2)}€ ({'+' if perf>=0 else ''}{round(perf,2)}%)")
    log.info(f"  PnL jour   : {'+' if etat.get('pnl_jour',0)>=0 else ''}{round(etat.get('pnl_jour',0),2)}€")
    log.info(f"  Trades     : {nb_trades} | Wins : {nb_wins} ({win_rate:.1f}%)")
    log.info(f"  Ouverts    : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}")
    log.info(f"  Pertes c.  : {etat.get('pertes_consecutives',0)}/{MAX_PERTES_CONSECUTIVES}")
    log.info(f"  Wins c.    : {etat.get('wins_consecutifs',0)}")
    log.info(f"  Gagné      : +{round(etat.get('total_gagne',0),2)}€")
    log.info(f"  Perdu      : -{round(etat.get('total_perdu',0),2)}€")
    log.info(f"  NET        : {'+' if etat.get('cumul_net',0)>=0 else ''}{round(etat.get('cumul_net',0),2)}€")
    if etat.get("historique"):
        log.info(f"\n  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "✅" if h["resultat"] == "GAGNE" else "❌"
            log.info(f"    {icone} {h['heure']} | {h['marche']} | {'+' if h['gain']>=0 else ''}{h['gain']}€")
    log.info(f"  {'='*55}")

# ═══════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE ASYNC
# ═══════════════════════════════════════════════════════════════
async def boucle_principale():
    global trades_lock
    trades_lock = asyncio.Lock()  # créé dans la boucle asyncio

    init_database()
    etat = charger_etat()

    for champ, valeur in [
        ("pnl_jour", 0.0), ("date_jour", ""), ("wins_consecutifs", 0),
        ("cooldown_until", 0), ("nb_skips", 0)
    ]:
        if champ not in etat:
            etat[champ] = valeur

    afficher_tableau_de_bord(etat)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        await telegram(session,
            f"🚀 <b>BOT HUMAIN VÉRONIQUE973 V2 DÉMARRÉ</b>\n"
            f"Capital : {round(etat['capital'],2)}€\n"
            f"Multi-trades : {MAX_TRADES_SIMULTANES} simultanés\n"
            f"Multi-timeframe | Lock paliers\n"
            f"Kill switch : {KILL_SWITCH_JOUR}€/jour\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        while True:
            try:
                reset_pnl_jour_si_nouveau_jour(etat)

                statut = verifier_protections(etat, etat["capital"])
                if statut == "RUINE":
                    await telegram(session, f"🚨 <b>SEUIL RUINE !</b>\nCapital : {etat['capital']}€\nBot arrêté !")
                    break
                if statut in ("KILL_SWITCH", "COOLDOWN"):
                    await asyncio.sleep(60)
                    etat = charger_etat()
                    continue

                session_nom, multiplicateur = get_session_actuelle()
                if session_nom == "nuit":
                    log.info("  🌙 Nuit UTC → bot en veille")
                    await asyncio.sleep(300)
                    continue

                # Vérifier slots disponibles
                async with trades_lock:
                    slots_libres = MAX_TRADES_SIMULTANES - len(trades_ouverts)
                    marches_disponibles = [m for m in MARCHES if m not in trades_ouverts]

                if slots_libres <= 0:
                    log.info(f"  {MAX_TRADES_SIMULTANES}/{MAX_TRADES_SIMULTANES} trades ouverts — attente...")
                    await asyncio.sleep(PAUSE_ENTRE_TRADES)
                    continue

                heure_utc = datetime.now(timezone.utc).strftime('%H:%M')
                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan — {session_nom.upper()} "
                         f"({heure_utc} UTC) | Slots libres : {slots_libres}/{MAX_TRADES_SIMULTANES}")

                # Scanner les marchés disponibles
                signaux = {}
                for marche in marches_disponibles:
                    direction, score, details = await analyser_multi_timeframe(session, marche)
                    if direction != "NEUTRE" and score >= 5:
                        signaux[marche] = {"direction": direction, "score": score, "details": details}
                    await asyncio.sleep(0.3)

                if not signaux:
                    log.info("  => Aucun signal.")
                    etat["nb_skips"] = etat.get("nb_skips", 0) + 1
                    sauvegarder_etat(etat)
                    await asyncio.sleep(PAUSE_ENTRE_TRADES)
                    continue

                # Trier par score et prendre les meilleurs selon slots libres
                meilleurs = sorted(signaux.items(), key=lambda x: x[1]["score"], reverse=True)[:slots_libres]

                for symbole, sig in meilleurs:
                    async with trades_lock:
                        if symbole in trades_ouverts:
                            continue
                        if len(trades_ouverts) >= MAX_TRADES_SIMULTANES:
                            break
                        trades_ouverts[symbole] = True

                    log.info(f"  ✅ Signal : {symbole} ({sig['direction']}) Score={sig['score']}/9")

                    # Lancer le trade en tâche parallèle
                    asyncio.create_task(
                        executer_trade(
                            session, symbole, sig["direction"],
                            etat["capital"],
                            sig["details"], etat, multiplicateur, etat
                        )
                    )

                await asyncio.sleep(PAUSE_ENTRE_TRADES)

            except KeyboardInterrupt:
                log.info("Bot arrêté.")
                break
            except Exception as e:
                log.error(f"Erreur inattendue : {e}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(boucle_principale())
