"""
╔══════════════════════════════════════════════════════════════════╗
║         BOT HUMAIN — VÉRONIQUE973                               ║
║  Multi-timeframe | Range vs Tendance | Heures intelligentes     ║
║  Lock profits paliers | Mise dynamique | Capital 500€           ║
╚══════════════════════════════════════════════════════════════════╝
"""

import requests
import time
import os
import logging
import pandas as pd
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from datetime import datetime, timezone
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
MISE_MAX_PCT            = 0.25       # jamais plus de 25% du capital
ATR_MULTIPLIER_STOP     = 2.5
ATR_MULTIPLIER_TARGET   = 5.0
CHECK_INTERVAL          = 10         # secondes entre chaque check prix
PAUSE_ENTRE_TRADES      = 120        # 2 min entre trades
TIMEOUT_TRADE           = 8 * 3600  # 8h max par trade

# Filtres signaux
RSI_OVERSOLD            = 30
RSI_OVERBOUGHT          = 70
ADX_RANGE_MAX           = 22         # en dessous = marché en range
ADX_TREND_MIN           = 25         # au dessus = marché en tendance
VOLUME_MINI             = 0.30       # ratio volume min

# Gestion mise dynamique
WINS_CONFIANCE          = 3          # nb wins consécutifs pour augmenter mise
BOOST_CONFIANCE         = 1.20       # +20% en confiance
REDUCTION_PERTES        = 0.50       # -50% après pertes
MIN_TRADES_KELLY        = 30
KELLY_FRACTION          = 0.25
KELLY_CAP               = 0.20

# Protections
KILL_SWITCH_JOUR        = -25.0      # €/jour
SEUIL_RUINE             = 300.0      # capital min absolu
MAX_PERTES_CONSECUTIVES = 2
COOLDOWN_PERTES         = 1800       # 30 min après 2 pertes

# Lock profits paliers
LOCK_PALIERS = [0.75, 1.50, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0]

# Heures de trading UTC
HEURES_LONDON    = (7, 12)    # 7h-12h UTC
HEURES_NEWYORK   = (13, 17)   # 13h-17h UTC
HEURES_NUIT      = (23, 6)    # 23h-6h UTC → bot dort

# Position dans le range
RANGE_BAS_SEUIL  = 0.20       # 20% du bas = zone achat
RANGE_HAUT_SEUIL = 0.80       # 80% du haut = zone vente
RANGE_HEURES     = 8          # calcul range sur 8h glissantes

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

MARCHES = [
    "XRPUSDT", "ATOMUSDT", "LINKUSDT",
    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT"
]

KRAKEN_SYMBOLS = {
    "XRPUSDT":  "XXRPZUSD",
    "ATOMUSDT": "ATOMUSD",
    "LINKUSDT": "LINKUSD",
    "ADAUSDT":  "ADAUSD",
    "SOLUSDT":  "SOLUSD",
    "AVAXUSDT": "AVAXUSD",
    "NEARUSDT": "NEARUSD",
    "DOTUSDT":  "DOTUSD"
}

log.info("=" * 60)
log.info("  BOT HUMAIN — VÉRONIQUE973")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER} | Mise base {MISE_BASE_PCT*100}%")
log.info(f"  Multi-timeframe : 4h → 1h → 15min")
log.info(f"  Range vs Tendance | Heures intelligentes")
log.info(f"  Lock paliers : {LOCK_PALIERS}")
log.info(f"  Kill switch : {KILL_SWITCH_JOUR}€/jour | Ruine : {SEUIL_RUINE}€")
log.info(f"  Telegram : {'ON' if TELEGRAM_TOKEN else 'OFF'}")
log.info("=" * 60)

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
#  DONNÉES MARCHÉ
# ═══════════════════════════════════════════════════════════════
def get_klines(symbole, interval=60, limite=100):
    """Récupère les bougies OHLCV depuis Kraken."""
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/OHLC"
    try:
        r = requests.get(url, params={"pair": kraken_symbol, "interval": interval}, timeout=15)
        data = r.json()
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

def get_prix_actuel(symbole):
    """Prix en temps réel depuis Kraken Ticker."""
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": kraken_symbol}, timeout=10
        )
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
    """Ratio volume dernière bougie fermée vs moyenne 24h."""
    try:
        volumes = df['volume'].tolist()
        if len(volumes) < 10:
            return 0.0
        moyenne = sum(volumes[-25:-1]) / 24
        recent  = volumes[-2]  # bougie fermée
        return round(recent / moyenne, 2) if moyenne > 0 else 0.0
    except:
        return 0.0

# ═══════════════════════════════════════════════════════════════
#  HEURES DE TRADING
# ═══════════════════════════════════════════════════════════════
def get_session_actuelle():
    """
    Retourne la session actuelle et le multiplicateur de mise.
    - 'london'   → x1.0 (pleine mise)
    - 'newyork'  → x1.0 (pleine mise)
    - 'asie'     → x0.5 (mise réduite)
    - 'nuit'     → None (bot dort)
    """
    heure_utc = datetime.now(timezone.utc).hour
    h_debut_nuit, h_fin_nuit = HEURES_NUIT

    # Nuit → bot dort
    if heure_utc >= h_debut_nuit or heure_utc < h_fin_nuit:
        return "nuit", None

    # London
    if HEURES_LONDON[0] <= heure_utc < HEURES_LONDON[1]:
        return "london", 1.0

    # New York
    if HEURES_NEWYORK[0] <= heure_utc < HEURES_NEWYORK[1]:
        return "newyork", 1.0

    # Autres heures → mise réduite
    return "asie", 0.5

# ═══════════════════════════════════════════════════════════════
#  POSITION DANS LE RANGE (intelligence humaine)
# ═══════════════════════════════════════════════════════════════
def analyser_position_range(df_1h):
    """
    Calcule où se trouve le prix dans le range des 8 dernières heures.
    Retourne: position (0.0=bas, 1.0=haut), haut, bas, prix_actuel
    """
    try:
        df_range = df_1h.tail(RANGE_HEURES)
        haut_session  = df_range['high'].max()
        bas_session   = df_range['low'].min()
        prix_actuel   = float(df_1h['close'].iloc[-1])
        range_total   = haut_session - bas_session
        if range_total <= 0:
            return 0.5, haut_session, bas_session, prix_actuel
        position = (prix_actuel - bas_session) / range_total
        return round(position, 3), round(haut_session, 6), round(bas_session, 6), round(prix_actuel, 6)
    except:
        return 0.5, 0, 0, 0

# ═══════════════════════════════════════════════════════════════
#  DÉTECTION RÉGIME DE MARCHÉ
# ═══════════════════════════════════════════════════════════════
def detecter_regime(df_4h, df_1h):
    """
    Détecte si le marché est en RANGE ou en TENDANCE.
    Utilise ADX 4h comme juge principal.
    """
    adx_4h = calc_adx(df_4h)
    adx_1h = calc_adx(df_1h)

    if adx_4h < ADX_RANGE_MAX and adx_1h < ADX_RANGE_MAX + 5:
        return "RANGE", adx_4h, adx_1h
    elif adx_4h >= ADX_TREND_MIN or adx_1h >= ADX_TREND_MIN:
        return "TENDANCE", adx_4h, adx_1h
    else:
        return "NEUTRE", adx_4h, adx_1h

# ═══════════════════════════════════════════════════════════════
#  ANALYSE MULTI-TIMEFRAME
# ═══════════════════════════════════════════════════════════════
def analyser_multi_timeframe(symbole):
    """
    Analyse complète sur 3 timeframes.
    Retourne: direction ("ACHAT"/"VENTE"/"NEUTRE"), score (0-10), details
    """
    # Récupérer les 3 timeframes
    df_4h  = get_klines(symbole, interval=240, limite=50)
    df_1h  = get_klines(symbole, interval=60,  limite=100)
    df_15m = get_klines(symbole, interval=15,  limite=100)

    if df_4h is None or df_1h is None or df_15m is None:
        log.warning(f"  {symbole} : données manquantes")
        return "NEUTRE", 0, {}

    if len(df_4h) < 20 or len(df_1h) < 30 or len(df_15m) < 30:
        return "NEUTRE", 0, {}

    # ── Régime de marché
    regime, adx_4h, adx_1h = detecter_regime(df_4h, df_1h)

    # ── Indicateurs par timeframe
    rsi_4h  = calc_rsi(df_4h)
    rsi_1h  = calc_rsi(df_1h)
    rsi_15m = calc_rsi(df_15m)

    ema20_1h  = calc_ema(df_1h, 20)
    ema50_1h  = calc_ema(df_1h, 50)
    prix_1h   = float(df_1h['close'].iloc[-1])

    atr_1h    = calc_atr(df_1h)
    vol_ratio = calc_volume_ratio(df_1h)

    # ── Position dans le range (intelligence humaine)
    position_range, haut, bas, prix_actuel = analyser_position_range(df_1h)

    details = {
        "regime":         regime,
        "adx_4h":         adx_4h,
        "adx_1h":         adx_1h,
        "rsi_4h":         rsi_4h,
        "rsi_1h":         rsi_1h,
        "rsi_15m":        rsi_15m,
        "ema20":          ema20_1h,
        "ema50":          ema50_1h,
        "prix":           prix_actuel,
        "atr":            atr_1h,
        "vol_ratio":      vol_ratio,
        "position_range": position_range,
        "haut_session":   haut,
        "bas_session":    bas,
    }

    # ── Filtre volume
    if vol_ratio < VOLUME_MINI:
        log.info(f"  {symbole} : Volume {vol_ratio:.2f}x < {VOLUME_MINI}x → skip")
        return "NEUTRE", 0, details

    # ════════════════════════════════════════════
    #  STRATÉGIE RANGE (marché calme)
    #  Logique humaine : acheter au bas, vendre au haut
    # ════════════════════════════════════════════
    if regime == "RANGE":
        score = 0
        direction = "NEUTRE"

        # Zone bas du range → chercher ACHAT
        if position_range <= RANGE_BAS_SEUIL:
            direction = "ACHAT"
            score += 3  # prix au bas de session
            if rsi_1h < RSI_OVERSOLD:
                score += 2  # RSI survendu 1h confirme
            if rsi_4h < 45:
                score += 1  # 4h pas suracheté
            if rsi_15m < RSI_OVERSOLD + 5:
                score += 1  # 15min aussi bas
            if vol_ratio >= 0.50:
                score += 1  # volume correct
            log.info(f"  {symbole} [RANGE] Bas session ({position_range:.0%}) | "
                     f"RSI 4h={rsi_4h} 1h={rsi_1h} 15m={rsi_15m} | Score={score}")

        # Zone haute du range → chercher VENTE
        elif position_range >= RANGE_HAUT_SEUIL:
            direction = "VENTE"
            score += 3  # prix au haut de session
            if rsi_1h > RSI_OVERBOUGHT:
                score += 2  # RSI suracheté 1h confirme
            if rsi_4h > 55:
                score += 1  # 4h pas survendu
            if rsi_15m > RSI_OVERBOUGHT - 5:
                score += 1  # 15min aussi haut
            if vol_ratio >= 0.50:
                score += 1  # volume correct
            log.info(f"  {symbole} [RANGE] Haut session ({position_range:.0%}) | "
                     f"RSI 4h={rsi_4h} 1h={rsi_1h} 15m={rsi_15m} | Score={score}")

        else:
            log.info(f"  {symbole} [RANGE] Position neutre {position_range:.0%} → skip")
            return "NEUTRE", 0, details

        # Score minimum 4/8 pour trader
        if score < 4:
            log.info(f"  {symbole} [RANGE] Score {score}/8 insuffisant → skip")
            return "NEUTRE", 0, details

        return direction, score, details

    # ════════════════════════════════════════════
    #  STRATÉGIE TENDANCE (marché directionnel)
    #  Logique humaine : suivre la tendance
    # ════════════════════════════════════════════
    elif regime == "TENDANCE":
        score = 0
        direction = "NEUTRE"

        # Tendance haussière → ACHAT
        if prix_1h > ema20_1h > ema50_1h:
            direction = "ACHAT"
            score += 3  # prix au dessus des EMAs
            if rsi_1h > 50 and rsi_1h < RSI_OVERBOUGHT:
                score += 2  # RSI haussier mais pas suracheté
            if rsi_4h > 50:
                score += 1  # 4h confirme
            if position_range > 0.40:
                score += 1  # pas au plus bas (momentum haussier)
            if vol_ratio >= 0.50:
                score += 1  # volume confirme
            log.info(f"  {symbole} [TENDANCE HAUSSIÈRE] Prix>{ema20_1h:.4f}>EMA50 | "
                     f"RSI 1h={rsi_1h} | Score={score}")

        # Tendance baissière → VENTE
        elif prix_1h < ema20_1h < ema50_1h:
            direction = "VENTE"
            score += 3  # prix en dessous des EMAs
            if rsi_1h < 50 and rsi_1h > RSI_OVERSOLD:
                score += 2  # RSI baissier mais pas survendu
            if rsi_4h < 50:
                score += 1  # 4h confirme
            if position_range < 0.60:
                score += 1  # pas au plus haut (momentum baissier)
            if vol_ratio >= 0.50:
                score += 1  # volume confirme
            log.info(f"  {symbole} [TENDANCE BAISSIÈRE] Prix<EMA20<{ema50_1h:.4f} | "
                     f"RSI 1h={rsi_1h} | Score={score}")

        else:
            log.info(f"  {symbole} [TENDANCE] EMAs croisées → pas clair → skip")
            return "NEUTRE", 0, details

        if score < 4:
            log.info(f"  {symbole} [TENDANCE] Score {score}/8 insuffisant → skip")
            return "NEUTRE", 0, details

        return direction, score, details

    else:
        log.info(f"  {symbole} [NEUTRE] ADX 4h={adx_4h} 1h={adx_1h} → marché indécis → skip")
        return "NEUTRE", 0, details

# ═══════════════════════════════════════════════════════════════
#  CHOIX DU MEILLEUR MARCHÉ
# ═══════════════════════════════════════════════════════════════
def choisir_meilleur_marche():
    session, multiplicateur = get_session_actuelle()

    if session == "nuit":
        log.info(f"  🌙 Nuit UTC → bot en veille")
        return None, "NEUTRE", {}, None

    heure_utc = datetime.now(timezone.utc).strftime('%H:%M')
    log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan — Session {session.upper()} "
             f"({heure_utc} UTC) | Mise x{multiplicateur}")

    signaux = {}
    for marche in MARCHES:
        direction, score, details = analyser_multi_timeframe(marche)
        if direction != "NEUTRE" and score >= 4:
            signaux[marche] = {"direction": direction, "score": score, "details": details}
        time.sleep(0.3)

    if not signaux:
        log.info("  => Aucun signal. On attend...")
        return None, "NEUTRE", {}, None

    # Prendre le signal avec le meilleur score
    meilleur = max(signaux.items(), key=lambda x: x[1]["score"])[0]
    sig      = signaux[meilleur]

    log.info(f"\n  ✅ MEILLEUR SIGNAL : {meilleur} ({sig['direction']}) | "
             f"Score {sig['score']}/8 | Régime: {sig['details'].get('regime')}")
    log.info(f"     RSI 4h={sig['details'].get('rsi_4h')} | "
             f"1h={sig['details'].get('rsi_1h')} | "
             f"15m={sig['details'].get('rsi_15m')} | "
             f"Range: {sig['details'].get('position_range', 0):.0%}")

    autres = [m for m in signaux if m != meilleur]
    if autres:
        log.info(f"     Autres signaux : {', '.join(autres)}")

    return meilleur, sig["direction"], sig["details"], multiplicateur

# ═══════════════════════════════════════════════════════════════
#  GESTION DE MISE DYNAMIQUE
# ═══════════════════════════════════════════════════════════════
def calculer_mise(capital, etat, multiplicateur_session):
    nb_trades    = etat.get("nb_trades", 0)
    nb_wins      = etat.get("nb_wins", 0)
    nb_losses    = etat.get("nb_losses", 0)
    wins_consec  = etat.get("wins_consecutifs", 0)
    pertes_consec = etat.get("pertes_consecutives", 0)
    avg_win_pct  = etat.get("avg_win_pct", 0)
    avg_loss_pct = etat.get("avg_loss_pct", 0)

    # Base
    mise = capital * MISE_BASE_PCT

    # Kelly si assez de trades
    if nb_trades >= MIN_TRADES_KELLY and avg_loss_pct > 0 and avg_win_pct > 0:
        win_rate   = nb_wins / nb_trades
        b          = avg_win_pct / avg_loss_pct
        kelly_full = (win_rate * b - (1 - win_rate)) / b
        kelly_frac = max(0, min(kelly_full * KELLY_FRACTION, KELLY_CAP))
        mise       = capital * kelly_frac

    # Modificateurs humains
    if pertes_consec >= 2:
        mise *= REDUCTION_PERTES
        log.info(f"  ⚠️ Mise réduite 50% ({pertes_consec} pertes consécutives)")

    elif wins_consec >= WINS_CONFIANCE:
        mise *= BOOST_CONFIANCE
        log.info(f"  💪 Mise boostée +20% ({wins_consec} wins consécutifs)")

    # Multiplicateur session (nuit réduit, London/NY plein)
    mise *= multiplicateur_session

    # Limites absolues
    mise = max(mise, MISE_MIN)
    mise = min(mise, capital * MISE_MAX_PCT)
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
#  EXÉCUTION DU TRADE
# ═══════════════════════════════════════════════════════════════
def executer_trade(symbole, direction, numero_trade, capital, details, etat, multiplicateur_session):
    prix_entree = get_prix_actuel(symbole)
    if prix_entree is None:
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
    regime = details.get("regime", "?")
    session, _ = get_session_actuelle()

    log.info(f"\n  {'='*55}")
    log.info(f"  TRADE #{numero_trade} [VÉRONIQUE973] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {'='*55}")
    log.info(f"  Symbole    : {symbole} ({direction})")
    log.info(f"  Régime     : {regime} | Session : {session.upper()}")
    log.info(f"  RSI        : 4h={details.get('rsi_4h')} | 1h={details.get('rsi_1h')} | 15m={details.get('rsi_15m')}")
    log.info(f"  Range      : {details.get('position_range', 0):.0%} | Haut={details.get('haut_session')} Bas={details.get('bas_session')}")
    log.info(f"  Prix       : {prix_entree}")
    log.info(f"  Stop       : {stop_loss} (-{round(distance_stop_pct,2)}%)")
    log.info(f"  Objectif   : {objectif_final}")
    log.info(f"  Mise       : {mise}€ × x{LEVIER} = {round(mise*LEVIER,2)}€\n")

    telegram(f"📊 <b>TRADE #{numero_trade} — VÉRONIQUE973</b>\n"
             f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
             f"Régime : {regime} | Session : {session.upper()}\n"
             f"RSI 4h={details.get('rsi_4h')} 1h={details.get('rsi_1h')}\n"
             f"Range : {details.get('position_range', 0):.0%}\n"
             f"Prix : {prix_entree} | Stop : {stop_loss}\n"
             f"Objectif : {objectif_final}\n"
             f"Mise : {mise}€ × x{LEVIER}\n"
             f"🔒 Lock paliers actifs")

    debut           = time.time()
    dernier_log     = 0
    prix_sortie     = prix_entree
    pnl_max_atteint = 0.0
    lock_actuel     = 0.0

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

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        # Lock paliers
        nouveau_lock = get_palier_lock(pnl_max_atteint)
        if nouveau_lock > lock_actuel:
            lock_actuel = nouveau_lock
            log.info(f"  🔒 LOCK {lock_actuel}€ garanti ! (PnL max={pnl_max_atteint:.2f}€)")
            telegram(f"🔒 <b>Lock {lock_actuel}€ garanti !</b>\n{symbole} | PnL={pnl:.2f}€")

        # Sortie lock
        if lock_actuel > 0 and pnl < lock_actuel and pnl_max_atteint > lock_actuel:
            duree = int((time.time() - debut) / 60)
            log.info(f"\n  🔒 SORTIE LOCK — +{lock_actuel}€ (max={pnl_max_atteint:.2f}€)")
            telegram(f"🔒 <b>SORTIE LOCK</b>\n{symbole}\nGain : <b>+{lock_actuel}€</b>\nDurée : {duree} min")
            return "GAGNE", lock_actuel, mise, {
                "prix_entree": prix_entree, "prix_sortie": prix_sortie,
                "stop_loss": stop_loss, "objectif": objectif_final,
                "duree_minutes": duree
            }

        # Objectif final
        atteint_final = (prix_actuel >= objectif_final if direction == "ACHAT"
                         else prix_actuel <= objectif_final)
        # Stop loss
        atteint_stop  = (prix_actuel <= stop_loss if direction == "ACHAT"
                         else prix_actuel >= stop_loss)

        duree = int((time.time() - debut) / 60)

        if time.time() - dernier_log >= 60:
            lock_flag = f" 🔒{lock_actuel}€" if lock_actuel > 0 else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl}€{lock_flag} | {duree}min")
            dernier_log = time.time()

        trade_info = {
            "prix_entree":   prix_entree,
            "prix_sortie":   prix_sortie,
            "stop_loss":     stop_loss,
            "objectif":      objectif_final,
            "duree_minutes": duree
        }

        if atteint_final:
            log.info(f"\n  🎯 OBJECTIF ! +{pnl}€")
            telegram(f"🎯 <b>OBJECTIF !</b>\n{symbole} +{pnl}€\nDurée : {duree} min")
            return "GAGNE", pnl, mise, trade_info

        if atteint_stop:
            resultat = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  🛑 STOP — {pnl}€")
            telegram(f"🛑 <b>STOP</b>\n{symbole} {pnl}€\nDurée : {duree} min")
            return resultat, pnl, mise, trade_info

        if time.time() - debut >= TIMEOUT_TRADE:
            resultat = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  ⏱ TIMEOUT — {'+' if pnl>=0 else ''}{pnl}€")
            telegram(f"⏱ <b>TIMEOUT</b>\n{symbole} {'+' if pnl>=0 else ''}{pnl}€\nDurée : {duree} min")
            return resultat, pnl, mise, trade_info

# ═══════════════════════════════════════════════════════════════
#  PROTECTIONS
# ═══════════════════════════════════════════════════════════════
def verifier_protections(etat, capital):
    # Seuil de ruine absolu
    if capital < SEUIL_RUINE:
        log.critical(f"🚨 SEUIL RUINE ! Capital {capital}€ < {SEUIL_RUINE}€ → ARRÊT")
        telegram(f"🚨 <b>SEUIL RUINE !</b>\nCapital : {capital}€\nBot arrêté définitivement !")
        return "RUINE"

    # Kill switch journalier
    pnl_jour = etat.get("pnl_jour", 0.0)
    if pnl_jour <= KILL_SWITCH_JOUR:
        log.warning(f"⚠️ KILL SWITCH JOUR — PnL {pnl_jour}€ ≤ {KILL_SWITCH_JOUR}€")
        telegram(f"⚠️ <b>KILL SWITCH</b>\nPnL jour : {pnl_jour}€\nPause jusqu'à demain")
        return "KILL_SWITCH"

    # Cooldown après pertes consécutives
    cooldown_until = etat.get("cooldown_until", 0)
    if time.time() < cooldown_until:
        restant = int((cooldown_until - time.time()) / 60)
        log.info(f"  ❄️ Cooldown — {restant} min restantes")
        time.sleep(60)
        return "COOLDOWN"

    # Fin de cooldown → reset pertes
    if cooldown_until > 0 and time.time() >= cooldown_until:
        log.info("  Fin cooldown → pertes consécutives remises à 0")
        etat["pertes_consecutives"] = 0
        etat["cooldown_until"]      = 0
        sauvegarder_etat(etat)

    # Déclencher cooldown si trop de pertes
    if etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:
        log.warning(f"  {MAX_PERTES_CONSECUTIVES} pertes consécutives → cooldown {COOLDOWN_PERTES//60} min")
        etat["cooldown_until"]      = int(time.time()) + COOLDOWN_PERTES
        etat["pertes_consecutives"] = 0
        sauvegarder_etat(etat)
        return "COOLDOWN"

    return "OK"

# ═══════════════════════════════════════════════════════════════
#  RÉINITIALISATION PNL JOURNALIER
# ═══════════════════════════════════════════════════════════════
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
    log.info(f"  BOT HUMAIN — VÉRONIQUE973")
    log.info(f"  {'='*55}")
    log.info(f"  Capital actuel : {round(etat['capital'],2)}€ ({'+' if perf>=0 else ''}{round(perf,2)}%)")
    log.info(f"  PnL jour       : {'+' if etat.get('pnl_jour',0)>=0 else ''}{round(etat.get('pnl_jour',0),2)}€")
    log.info(f"  Trades total   : {nb_trades} | Victoires : {nb_wins} ({win_rate:.1f}%)")
    log.info(f"  Pertes consec. : {etat.get('pertes_consecutives',0)}/{MAX_PERTES_CONSECUTIVES}")
    log.info(f"  Wins consec.   : {etat.get('wins_consecutifs',0)}")
    log.info(f"  Total gagné    : +{round(etat.get('total_gagne',0),2)}€")
    log.info(f"  Total perdu    : -{round(etat.get('total_perdu',0),2)}€")
    log.info(f"  BÉNÉFICE NET   : {'+' if etat.get('cumul_net',0)>=0 else ''}{round(etat.get('cumul_net',0),2)}€")
    if etat.get("historique"):
        log.info(f"\n  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "✅" if h["resultat"] == "GAGNE" else "❌"
            log.info(f"    {icone} {h['heure']} | {h['marche']} | {h['direction']} | "
                     f"{'+' if h['gain']>=0 else ''}{h['gain']}€")
    log.info(f"  {'='*55}")

def envoyer_rapport_telegram(etat):
    nb_trades = etat.get("nb_trades", 0)
    nb_wins   = etat.get("nb_wins", 0)
    win_rate  = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0
    perf      = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    telegram(f"📈 <b>RAPPORT VÉRONIQUE973</b>\n"
             f"Capital : <b>{round(etat['capital'],2)}€</b> ({'+' if perf>=0 else ''}{round(perf,2)}%)\n"
             f"PnL jour : {'+' if etat.get('pnl_jour',0)>=0 else ''}{round(etat.get('pnl_jour',0),2)}€\n"
             f"Trades : {nb_trades} | WR : {round(win_rate,1)}%\n"
             f"<b>NET : {'+' if etat.get('cumul_net',0)>=0 else ''}{round(etat.get('cumul_net',0),2)}€</b>")

# ═══════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════
def demarrer_bot():
    log.info(f"DÉMARRAGE BOT HUMAIN VÉRONIQUE973 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    init_database()
    etat = charger_etat()

    # Initialiser les champs spécifiques si absents
    for champ, valeur in [
        ("pnl_jour", 0.0), ("date_jour", ""), ("wins_consecutifs", 0),
        ("cooldown_until", 0), ("nb_skips", 0)
    ]:
        if champ not in etat:
            etat[champ] = valeur

    afficher_tableau_de_bord(etat)
    telegram(f"🚀 <b>BOT HUMAIN VÉRONIQUE973 DÉMARRÉ</b>\n"
             f"Capital : {round(etat['capital'],2)}€\n"
             f"Multi-timeframe | Lock paliers\n"
             f"Kill switch : {KILL_SWITCH_JOUR}€/jour\n"
             f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        try:
            reset_pnl_jour_si_nouveau_jour(etat)

            statut = verifier_protections(etat, etat["capital"])
            if statut == "RUINE":
                break
            if statut in ("KILL_SWITCH", "COOLDOWN"):
                time.sleep(60)
                etat = charger_etat()
                continue

            symbole, direction, details, multiplicateur = choisir_meilleur_marche()

            if direction == "NEUTRE" or symbole is None:
                etat["nb_skips"] = etat.get("nb_skips", 0) + 1
                sauvegarder_etat(etat)
                session, _ = get_session_actuelle()
                attente = 300 if session == "nuit" else PAUSE_ENTRE_TRADES
                log.info(f"  Nouvelle analyse dans {attente//60} minute(s)...")
                time.sleep(attente)
                continue

            etat["nb_trades"] = etat.get("nb_trades", 0) + 1
            resultat, gain, mise, trade_info = executer_trade(
                symbole, direction, etat["nb_trades"],
                etat["capital"], details, etat, multiplicateur or 1.0
            )

            if resultat == "ERREUR":
                etat["nb_trades"] -= 1
                time.sleep(PAUSE_ENTRE_TRADES)
                continue

            etat["capital"]    = round(etat["capital"] + gain, 2)
            etat["cumul_net"]  = round(etat["capital"] - CAPITAL_INITIAL, 2)
            etat["pnl_jour"]   = round(etat.get("pnl_jour", 0) + gain, 2)

            if resultat == "GAGNE":
                etat["nb_wins"]             = etat.get("nb_wins", 0) + 1
                etat["total_gagne"]         = round(etat.get("total_gagne", 0) + gain, 2)
                etat["pertes_consecutives"] = 0
                etat["wins_consecutifs"]    = etat.get("wins_consecutifs", 0) + 1
                gain_pct = (gain / max(mise * LEVIER, 1)) * 100
                n = etat["nb_wins"]
                etat["avg_win_pct"] = round(
                    (etat.get("avg_win_pct", 0) * (n-1) + gain_pct) / n, 4
                )
            else:
                etat["nb_losses"]           = etat.get("nb_losses", 0) + 1
                etat["total_perdu"]         = round(etat.get("total_perdu", 0) + abs(gain), 2)
                etat["pertes_consecutives"] = etat.get("pertes_consecutives", 0) + 1
                etat["wins_consecutifs"]    = 0
                perte_pct = (abs(gain) / max(mise * LEVIER, 1)) * 100
                n = etat["nb_losses"]
                etat["avg_loss_pct"] = round(
                    (etat.get("avg_loss_pct", 0) * (n-1) + perte_pct) / n, 4
                )

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
                'score':         details.get('score', None),
                'adx':           details.get('adx_1h'),
                'atr':           details.get('atr'),
                'rsi':           details.get('rsi_1h'),
            })

            sauvegarder_etat(etat)
            etat.setdefault("historique", []).append({
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
            log.info(f"  Pause {PAUSE_ENTRE_TRADES//60} min avant prochain trade...")
            time.sleep(PAUSE_ENTRE_TRADES)

        except KeyboardInterrupt:
            log.info("Bot arrêté.")
            break
        except Exception as e:
            log.error(f"Erreur inattendue : {e}")
            time.sleep(60)

if __name__ == "__main__":
    demarrer_bot()
