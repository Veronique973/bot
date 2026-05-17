"""
╔══════════════════════════════════════════════════════════════════╗
║         BOT HUMAIN — VÉRONIQUE973 V4                            ║
║  Mean Reversion 0.60% | Trailing Stop Progressif Sans Plafond  ║
║  Lock profits | 3 trades simultanés | Capital 500€             ║
║  Architecture async aiohttp                                     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import aiohttp
import os
import logging
import time
from datetime import datetime
import pandas as pd
from ta.volatility import AverageTrueRange
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
MISE_BASE_PCT           = 0.10
MISE_MIN                = 10.0
MISE_MAX_PCT            = 0.25
CHECK_INTERVAL          = 10         # secondes entre chaque check
PAUSE_ENTRE_TRADES      = 120        # 2 min entre scans
TIMEOUT_TRADE           = 12 * 3600  # 12h max par trade
MAX_TRADES_SIMULTANES   = 3

# ── Détection signal mean reversion
SEUIL_MOUVEMENT_PCT     = 0.60   # chute/montée de 0.60% sur 1 bougie 15min
NB_BOUGIES_SIGNAL       = 1      # 1 bougie 15min fermée suffit
VOLUME_MINI             = 0.25   # volume min vs moyenne 24h

# ── Stop initial
ATR_STOP_INITIAL        = 2.50

# ── Trailing stop progressif sans plafond
# (pnl_min, atr_multiplicateur)
TRAILING_PALIERS = [
    (100.0, 0.10),   # PnL > 100€ → ATR × 0.10 (très serré)
    ( 50.0, 0.15),   # PnL > 50€  → ATR × 0.15
    ( 25.0, 0.20),   # PnL > 25€  → ATR × 0.20
    ( 12.0, 0.30),   # PnL > 12€  → ATR × 0.30
    (  8.0, 0.50),   # PnL > 8€   → ATR × 0.50
    (  5.0, 0.70),   # PnL > 5€   → ATR × 0.70
    (  3.0, 1.00),   # PnL > 3€   → ATR × 1.00
    (  1.5, 1.50),   # PnL > 1.5€ → ATR × 1.50
    (  0.75,2.00),   # PnL > 0.75€→ ATR × 2.00
    (  0.0, 2.50),   # PnL > 0€   → ATR × 2.50 (stop initial)
]

def get_trailing_multiplicateur(pnl):
    """Retourne le multiplicateur ATR selon le PnL actuel."""
    for seuil, mult in TRAILING_PALIERS:
        if pnl >= seuil:
            return mult
    return ATR_STOP_INITIAL

# ── Gestion mise dynamique
WINS_CONFIANCE          = 3
BOOST_CONFIANCE         = 1.20
REDUCTION_PERTES        = 0.50
MIN_TRADES_KELLY        = 30
KELLY_FRACTION          = 0.25
KELLY_CAP               = 0.20

# ── Protections
KILL_SWITCH_JOUR        = -25.0
SEUIL_RUINE             = 300.0
MAX_PERTES_CONSECUTIVES = 2
COOLDOWN_PERTES         = 1800   # 30 min

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
#  ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════
trades_ouverts = {}
trades_lock    = None  # initialisé dans boucle_principale()

log.info("=" * 60)
log.info("  BOT HUMAIN — VÉRONIQUE973 V4")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER}")
log.info(f"  Marchés : {len(MARCHES)} cryptos | Max {MAX_TRADES_SIMULTANES} trades")
log.info(f"  Signal : chute/montée ≥ {SEUIL_MOUVEMENT_PCT}% sur {NB_BOUGIES_SIGNAL} bougies 15min")
log.info(f"  Trailing stop progressif : {len(TRAILING_PALIERS)} paliers sans plafond")
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
async def get_klines(session, symbole, interval=15, limite=100):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/OHLC"
    try:
        async with session.get(
            url,
            params={"pair": kraken_symbol, "interval": interval},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
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
#  INDICATEURS
# ═══════════════════════════════════════════════════════════════
def calc_atr(df, periode=14):
    try:
        val = AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'], window=periode
        ).average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0.0
    except:
        return 0.0

def calc_volume_ratio(df):
    """Ratio bougie fermée vs moyenne 24h."""
    try:
        volumes = df['volume'].tolist()
        if len(volumes) < 10:
            return 0.0
        moyenne = sum(volumes[-25:-1]) / 24
        recent  = volumes[-2]
        return round(recent / moyenne, 2) if moyenne > 0 else 0.0
    except:
        return 0.0

def detecter_mouvement(df_15m):
    """
    Détecte si le prix a chuté ou monté de SEUIL_MOUVEMENT_PCT%
    sur les NB_BOUGIES_SIGNAL dernières bougies 15min fermées.

    Retourne:
      "ACHAT"  si chute >= seuil (on anticipe un rebond)
      "VENTE"  si montée >= seuil (on anticipe une correction)
      "NEUTRE" sinon
    """
    try:
        # On prend les bougies fermées — on exclut la dernière (en cours)
        closes = df_15m['close'].tolist()[:-1]
        if len(closes) < NB_BOUGIES_SIGNAL + 1:
            return "NEUTRE", 0.0

        prix_debut = closes[-(NB_BOUGIES_SIGNAL + 1)]
        prix_fin   = closes[-1]

        if prix_debut <= 0:
            return "NEUTRE", 0.0

        variation_pct = (prix_fin - prix_debut) / prix_debut * 100

        if variation_pct <= -SEUIL_MOUVEMENT_PCT:
            # Chute → on achète (rebond attendu)
            return "ACHAT", abs(variation_pct)
        elif variation_pct >= SEUIL_MOUVEMENT_PCT:
            # Montée → on vend (correction attendue)
            return "VENTE", abs(variation_pct)
        else:
            return "NEUTRE", abs(variation_pct)
    except:
        return "NEUTRE", 0.0

# ═══════════════════════════════════════════════════════════════
#  ANALYSE MARCHÉ
# ═══════════════════════════════════════════════════════════════
async def analyser_marche(session, symbole):
    """
    Stratégie Mean Reversion :
    1. Détecte une chute/montée de 0.60% sur 1 bougie 15min fermée
    2. Filtre volume > 0.25x
    """
    df_15m = await get_klines(session, symbole, interval=15, limite=50)

    if df_15m is None or len(df_15m) < 20:
        return "NEUTRE", {}

    # ── Détection mouvement
    direction, variation_pct = detecter_mouvement(df_15m)
    if direction == "NEUTRE":
        log.info(f"  {symbole} : variation {variation_pct:.2f}% < {SEUIL_MOUVEMENT_PCT}% → skip")
        return "NEUTRE", {}

    # ── ATR et volume sur 15min
    atr_15m   = calc_atr(df_15m)
    vol_ratio = calc_volume_ratio(df_15m)

    # ── Filtre volume
    if vol_ratio < VOLUME_MINI:
        log.info(f"  {symbole} : Vol {vol_ratio:.2f}x < {VOLUME_MINI}x → skip")
        return "NEUTRE", {}

    details = {
        "atr":           atr_15m,
        "vol_ratio":     vol_ratio,
        "variation_pct": variation_pct,
    }

    log.info(f"  {symbole} ✅ {direction} | Variation={variation_pct:.2f}% | Vol={vol_ratio:.2f}x")
    return direction, details

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
#  EXÉCUTION D'UN TRADE
# ═══════════════════════════════════════════════════════════════
async def executer_trade(session, symbole, direction, capital, details, etat, multiplicateur_session, etat_global):
    prix_entree = await get_prix_actuel(session, symbole)
    if prix_entree is None:
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        return "ERREUR", 0, 0, {}

    atr  = details.get("atr", 0)
    mise = calculer_mise(capital, etat, multiplicateur_session)

    # Stop initial basé sur ATR × 2.50
    if direction == "ACHAT":
        stop_initial   = round(prix_entree - atr * ATR_STOP_INITIAL, 8)
        objectif_final = round(prix_entree + atr * ATR_STOP_INITIAL * 2, 8)
    else:
        stop_initial   = round(prix_entree + atr * ATR_STOP_INITIAL, 8)
        objectif_final = round(prix_entree - atr * ATR_STOP_INITIAL * 2, 8)

    # Numéro de trade sous lock
    async with trades_lock:
        etat_global["nb_trades"] = etat_global.get("nb_trades", 0) + 1
        numero_trade = etat_global["nb_trades"]

    log.info(f"\n  {'='*55}")
    log.info(f"  TRADE #{numero_trade} [VÉRONIQUE973 V4] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {symbole} ({direction})")
    log.info(f"  Variation : {details.get('variation_pct', 0):.2f}% | Vol={details.get('vol_ratio', 0):.2f}x")
    log.info(f"  Prix : {prix_entree} | Stop initial : {stop_initial}")
    log.info(f"  Mise : {mise}€ × x{LEVIER} = {round(mise*LEVIER,2)}€")
    log.info(f"  Trailing : {len(TRAILING_PALIERS)} paliers progressifs sans plafond")
    log.info(f"  Trades ouverts : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n")

    await telegram(session,
        f"📊 <b>TRADE #{numero_trade} — VÉRONIQUE973 V4</b>\n"
        f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
        f"Variation : {details.get('variation_pct', 0):.2f}%\n"
        f"Volume : {details.get('vol_ratio', 0):.2f}x\n"
        f"Prix : {prix_entree} | Stop : {stop_initial}\n"
        f"Mise : {mise}€ × x{LEVIER}\n"
        f"Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n"
        f"🎯 Trailing progressif sans plafond"
    )

    debut           = time.time()
    dernier_log     = 0
    prix_sortie     = prix_entree
    meilleur_prix   = prix_entree
    stop_actuel     = stop_initial
    niveau_actuel   = ATR_STOP_INITIAL
    pnl_max_atteint = 0.0
    resultat_final  = None
    gain_final      = 0.0

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        prix_actuel = await get_prix_actuel(session, symbole)
        if prix_actuel is None:
            continue

        prix_sortie = prix_actuel

        # ── Calcul PnL
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        # ── Trailing stop progressif
        multiplicateur = get_trailing_multiplicateur(pnl_max_atteint)
        distance_trailing = atr * multiplicateur

        if direction == "ACHAT":
            if prix_actuel > meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix - distance_trailing, 8)
            if nouveau_stop > stop_actuel:
                stop_actuel = nouveau_stop
        else:
            if prix_actuel < meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix + distance_trailing, 8)
            if nouveau_stop < stop_actuel:
                stop_actuel = nouveau_stop

        # ── Log changement de palier
        if multiplicateur != niveau_actuel:
            if direction == "ACHAT":
                gain_protege = round((stop_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
            else:
                gain_protege = round((prix_entree - stop_actuel) / prix_entree * mise * LEVIER, 2)
            log.info(f"  📈 PALIER [{symbole}] PnL={pnl:.2f}€ → ATR×{multiplicateur} | "
                     f"Stop={stop_actuel} | Protège≈{gain_protege:.2f}€")
            await telegram(session,
                f"📈 <b>Nouveau palier trailing</b>\n"
                f"{symbole} | PnL={'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                f"Trailing : ATR×{multiplicateur}\n"
                f"Gain protégé : ≈{gain_protege:.2f}€"
            )
            niveau_actuel = multiplicateur

        # ── Vérification stop
        atteint_stop = (prix_actuel <= stop_actuel if direction == "ACHAT"
                        else prix_actuel >= stop_actuel)

        duree = int((time.time() - debut) / 60)

        if time.time() - dernier_log >= 60:
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl:.2f}€ | "
                     f"Stop={stop_actuel} (ATR×{multiplicateur}) | {duree}min")
            dernier_log = time.time()

        trade_info = {
            "prix_entree":   prix_entree,
            "prix_sortie":   prix_sortie,
            "stop_loss":     stop_initial,
            "objectif":      objectif_final,
            "duree_minutes": duree
        }

        if atteint_stop:
            resultat_final = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  🛑 STOP TRAILING [{symbole}] {'+' if pnl>=0 else ''}{pnl:.2f}€ "
                     f"(max={pnl_max_atteint:.2f}€) | {duree}min")
            await telegram(session,
                f"🛑 <b>STOP TRAILING</b>\n"
                f"{symbole} {direction}\n"
                f"Résultat : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                f"PnL max atteint : +{pnl_max_atteint:.2f}€\n"
                f"Durée : {duree} min"
            )
            gain_final = pnl
            break

        if time.time() - debut >= TIMEOUT_TRADE:
            resultat_final = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  ⏱ TIMEOUT [{symbole}] {'+' if pnl>=0 else ''}{pnl:.2f}€")
            await telegram(session,
                f"⏱ <b>TIMEOUT</b>\n{symbole} {'+' if pnl>=0 else ''}{pnl:.2f}€\nDurée : {duree} min"
            )
            gain_final = pnl
            break

    # Libérer le marché
    async with trades_lock:
        trades_ouverts.pop(symbole, None)

    # Mettre à jour l'état global sous lock
    async with trades_lock:
        etat_global["capital"]   = round(etat_global["capital"] + gain_final, 2)
        etat_global["cumul_net"] = round(etat_global["capital"] - CAPITAL_INITIAL, 2)
        etat_global["pnl_jour"]  = round(etat_global.get("pnl_jour", 0) + gain_final, 2)

        if resultat_final == "GAGNE":
            etat_global["nb_wins"]             = etat_global.get("nb_wins", 0) + 1
            etat_global["total_gagne"]         = round(etat_global.get("total_gagne", 0) + gain_final, 2)
            etat_global["pertes_consecutives"] = 0
            etat_global["wins_consecutifs"]    = etat_global.get("wins_consecutifs", 0) + 1
            n        = etat_global["nb_wins"]
            gain_pct = (gain_final / max(mise * LEVIER, 1)) * 100
            etat_global["avg_win_pct"] = round(
                (etat_global.get("avg_win_pct", 0) * (n - 1) + gain_pct) / n, 4
            )
        else:
            etat_global["nb_losses"]           = etat_global.get("nb_losses", 0) + 1
            etat_global["total_perdu"]         = round(etat_global.get("total_perdu", 0) + abs(gain_final), 2)
            etat_global["pertes_consecutives"] = etat_global.get("pertes_consecutives", 0) + 1
            etat_global["wins_consecutifs"]    = 0
            n         = etat_global["nb_losses"]
            perte_pct = (abs(gain_final) / max(mise * LEVIER, 1)) * 100
            etat_global["avg_loss_pct"] = round(
                (etat_global.get("avg_loss_pct", 0) * (n - 1) + perte_pct) / n, 4
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
        'marche':        symbole,
        'direction':     direction,
        'resultat':      resultat_final,
        'prix_entree':   trade_info['prix_entree'],
        'prix_sortie':   trade_info['prix_sortie'],
        'stop_loss':     trade_info['stop_loss'],
        'objectif':      trade_info['objectif'],
        'mise':          mise,
        'gain':          round(gain_final, 2),
        'capital_apres': etat_global['capital'],
        'duree_minutes': trade_info['duree_minutes'],
        'score':         None,
        'adx':           None,
        'atr':           details.get('atr'),
        'rsi':           details.get('rsi_2h'),
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
    if etat.get("pnl_jour", 0.0) <= KILL_SWITCH_JOUR:
        log.warning(f"⚠️ KILL SWITCH — PnL jour {etat.get('pnl_jour', 0)}€")
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
        log.info("  📅 Nouveau jour — PnL remis à 0")

# ═══════════════════════════════════════════════════════════════
#  TABLEAU DE BORD
# ═══════════════════════════════════════════════════════════════
def afficher_tableau_de_bord(etat):
    nb_trades = etat.get("nb_trades", 0)
    nb_wins   = etat.get("nb_wins", 0)
    win_rate  = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0
    perf      = (etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    log.info(f"\n  {'='*55}")
    log.info(f"  BOT HUMAIN — VÉRONIQUE973 V4")
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
        log.info("  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "✅" if h["resultat"] == "GAGNE" else "❌"
            log.info(f"    {icone} {h['heure']} | {h['marche']} | {'+' if h['gain']>=0 else ''}{h['gain']}€")
    log.info(f"  {'='*55}")

# ═══════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════
async def boucle_principale():
    global trades_lock
    trades_lock = asyncio.Lock()

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
            f"🚀 <b>BOT HUMAIN VÉRONIQUE973 V4 DÉMARRÉ</b>\n"
            f"Capital : {round(etat['capital'],2)}€\n"
            f"Signal : chute/montée ≥ {SEUIL_MOUVEMENT_PCT}% sur {NB_BOUGIES_SIGNAL} bougies\n"
            f"Trailing stop progressif sans plafond\n"
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

                async with trades_lock:
                    slots_libres        = MAX_TRADES_SIMULTANES - len(trades_ouverts)
                    marches_disponibles = [m for m in MARCHES if m not in trades_ouverts]

                if slots_libres <= 0:
                    log.info(f"  {MAX_TRADES_SIMULTANES}/{MAX_TRADES_SIMULTANES} trades ouverts — attente...")
                    await asyncio.sleep(PAUSE_ENTRE_TRADES)
                    continue

                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan "
                         f"| Slots : {slots_libres}/{MAX_TRADES_SIMULTANES}")

                signaux = {}
                for marche in marches_disponibles:
                    direction, details = await analyser_marche(session, marche)
                    if direction != "NEUTRE":
                        signaux[marche] = {"direction": direction, "details": details}
                    await asyncio.sleep(0.3)

                if not signaux:
                    log.info("  => Aucun signal.")
                    etat["nb_skips"] = etat.get("nb_skips", 0) + 1
                    sauvegarder_etat(etat)
                    await asyncio.sleep(PAUSE_ENTRE_TRADES)
                    continue

                # Trier par variation la plus forte
                meilleurs = sorted(
                    signaux.items(),
                    key=lambda x: x[1]["details"].get("variation_pct", 0),
                    reverse=True
                )[:slots_libres]

                for symbole, sig in meilleurs:
                    async with trades_lock:
                        if symbole in trades_ouverts:
                            continue
                        if len(trades_ouverts) >= MAX_TRADES_SIMULTANES:
                            break
                        trades_ouverts[symbole] = True

                    log.info(f"  ✅ {symbole} ({sig['direction']}) "
                             f"Variation={sig['details'].get('variation_pct', 0):.2f}%")

                    asyncio.create_task(
                        executer_trade(
                            session, symbole, sig["direction"],
                            etat["capital"],
                            sig["details"], etat, 1.0, etat
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
