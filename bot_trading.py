
Action: file_editor view /app/bot_v8_corrected.py
Observation: [Showing lines 1-623 of 623 total] /app/bot_v8_corrected.py:
1|"""
2|╔══════════════════════════════════════════════════════════════╗
3|║           BOT MEAN REVERSION V8 — ADAPTATIF                 ║
4|║   Base : V7.3 corrigé + détection régime                    ║
5|║   Trailing Stop CONTINU | Paliers : +0.75€, +3€, +7.50€...  ║
6|╚══════════════════════════════════════════════════════════════╝
7|
8|VERSION CORRIGÉE — Corrections appliquées :
9|  [FIX #1] verifier_kill_switch : le kill switch ne se déclenchait
10|           JAMAIS au premier déclenchement car la branche `else`
11|           réinitialisait pertes_consecutives à 0 dès que
12|           pause_until == 0 (état initial). Ajout de la garde
13|           `pause_until > 0` + réinitialisation de pause_until.
14|  [FIX #2] demarrer_bot : etat['historique'].append() était
15|           appelé APRÈS sauvegarder_etat() → la dernière entrée
16|           d'historique n'était jamais persistée. Ordre inversé.
17|  [FIX #3] analyser_marche : si calculer_atr() renvoie 0 (échec
18|           indicateur), le stop collerait au prix d'entrée et
19|           déclencherait au moindre tick. Skip si ATR <= 0.
20|  [FIX #4] simuler_trade : win_rate était calculé avec nb_trades
21|           déjà incrémenté (incluant le trade en cours). Utilise
22|           maintenant trades_termines = nb_trades - 1.
23|"""
24|
25|import requests
26|import time
27|import os
28|import logging
29|import pandas as pd
30|from ta.trend import ADXIndicator
31|from ta.volatility import AverageTrueRange
32|from ta.momentum import RSIIndicator
33|from datetime import datetime
34|from database import init_database, charger_etat, sauvegarder_etat, enregistrer_trade
35|
36|logging.basicConfig(
37|    level=logging.INFO,
38|    format='%(asctime)s - %(levelname)s - %(message)s',
39|    handlers=[logging.StreamHandler()]
40|)
41|log = logging.getLogger(__name__)
42|
43|CAPITAL_INITIAL         = 215.0
44|LEVIER                  = 10
45|MISE_FIXE_PCT           = 0.20
46|KELLY_FRACTION          = 0.25
47|KELLY_CAP               = 0.20
48|MIN_TRADES_KELLY        = 30
49|ATR_MULTIPLIER          = 2.5
50|RATIO_RR                = 2.0
51|RATIO_PARTIEL           = 1.0
52|PAUSE                   = 120
53|CHECK_INTERVAL          = 10
54|TIMEOUT_TRADE           = 12 * 3600
55|RSI_ACHAT               = 30
56|RSI_VENTE               = 70
57|VOLUME_MINI             = 0.40
58|ADX_MAX                 = 40
59|MAX_PERTES_CONSECUTIVES = 2
60|SEUIL_RUINE             = 0.30
61|PAUSE_DUREE             = 43200      # 12h
62|
63|TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
64|TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
65|
66|# Paliers de Trailing Stop (protection progressive, premier palier à +0.75€)
67|TRAILING_NIVEAUX = [
68|    (100, 0.05),
69|    ( 75, 0.07),
70|    ( 50, 0.10),
71|    ( 35, 0.15),
72|    ( 25, 0.20),
73|    ( 18, 0.30),
74|    ( 12, 0.50),
75|    (7.5, 0.80),
76|    (  3, 1.50),
77|    (0.75, 2.00),
78|    (  0, 2.50),
79|]
80|
81|def get_multiplicateur_atr(pnl):
82|    for seuil, mult in TRAILING_NIVEAUX:
83|        if pnl >= seuil:
84|            return mult
85|    return 2.50
86|
87|MARCHES = [
88|    "BTCUSDT", "ETHUSDT", "XRPUSDT", "ATOMUSDT", "LINKUSDT",
89|    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT"
90|]
91|
92|KRAKEN_SYMBOLS = {
93|    "BTCUSDT":  "XXBTZUSD",
94|    "ETHUSDT":  "XETHZUSD",
95|    "XRPUSDT":  "XXRPZUSD",
96|    "ATOMUSDT": "ATOMUSD",
97|    "LINKUSDT": "LINKUSD",
98|    "ADAUSDT":  "ADAUSD",
99|    "SOLUSDT":  "SOLUSD",
100|    "AVAXUSDT": "AVAXUSD",
101|    "NEARUSDT": "NEARUSD",
102|    "DOTUSDT":  "DOTUSD"
103|}
104|
105|# ══════════════════════════════════════════════════════════════
106|# V8 — DÉTECTION DU RÉGIME DE MARCHÉ
107|# ══════════════════════════════════════════════════════════════
108|
109|V8_ADX_TENDANCE    = 25
110|V8_ATR_VOLATILE    = 3.0
111|V8_SCAN_INTERVAL   = 3600
112|
113|regime_actuel = {
114|    "mode": "RANGE",
115|    "mise_pct": 0.20,
116|    "rsi_achat": 27,
117|    "rsi_vente": 73,
118|    "derniere_maj": 0
119|}
120|
121|def detecter_regime():
122|    adx_values = []
123|    atr_pct_values = []
124|    for symbole in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
125|        df = get_klines(symbole, limite=50)
126|        if df is None or len(df) < 20:
127|            continue
128|        try:
129|            adx_ind = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
130|            atr_ind = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14)
131|            adx_val = float(adx_ind.adx().iloc[-1])
132|            atr_val = float(atr_ind.average_true_range().iloc[-1])
133|            prix    = float(df['close'].iloc[-1])
134|            atr_pct = (atr_val / prix) * 100
135|            if not pd.isna(adx_val):
136|                adx_values.append(adx_val)
137|            if not pd.isna(atr_pct):
138|                atr_pct_values.append(atr_pct)
139|        except:
140|            pass
141|        time.sleep(0.5)
142|    if not adx_values:
143|        return
144|    adx_moyen = sum(adx_values) / len(adx_values)
145|    atr_moyen = sum(atr_pct_values) / len(atr_pct_values) if atr_pct_values else 0
146|    ancien_mode = regime_actuel["mode"]
147|    if atr_moyen > V8_ATR_VOLATILE:
148|        regime_actuel["mode"]      = "VOLATILE"
149|        regime_actuel["mise_pct"]  = 0.10
150|        regime_actuel["rsi_achat"] = 27
151|        regime_actuel["rsi_vente"] = 73
152|    elif adx_moyen > V8_ADX_TENDANCE:
153|        regime_actuel["mode"]      = "TENDANCE"
154|        regime_actuel["mise_pct"]  = 0.20
155|        regime_actuel["rsi_achat"] = 25
156|        regime_actuel["rsi_vente"] = 75
157|    else:
158|        regime_actuel["mode"]      = "RANGE"
159|        regime_actuel["mise_pct"]  = 0.20
160|        regime_actuel["rsi_achat"] = 27
161|        regime_actuel["rsi_vente"] = 73
162|    regime_actuel["derniere_maj"] = int(time.time())
163|    log.info(f"\n  {'='*55}")
164|    log.info(f"  [V8] RÉGIME DÉTECTÉ : {regime_actuel['mode']}")
165|    log.info(f"  ADX moyen : {round(adx_moyen,1)} | ATR moyen : {round(atr_moyen,2)}%")
166|    log.info(f"  Mise : {regime_actuel['mise_pct']*100}% | RSI : {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}")
167|    log.info(f"  {'='*55}")
168|    if ancien_mode != regime_actuel["mode"]:
169|        emoji = "📊" if regime_actuel["mode"] == "RANGE" else "⚡" if regime_actuel["mode"] == "VOLATILE" else "📈"
170|        telegram(f"{emoji} <b>CHANGEMENT DE RÉGIME</b>\n{ancien_mode} → <b>{regime_actuel['mode']}</b>\nADX : {round(adx_moyen,1)} | ATR : {round(atr_moyen,2)}%\nMise : {regime_actuel['mise_pct']*100}% | RSI : {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}")
171|
172|def telegram(message):
173|    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
174|        return
175|    try:
176|        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
177|        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
178|    except Exception as e:
179|        log.error(f"Erreur Telegram : {e}")
180|
181|def get_prix_actuel(symbole):
182|    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
183|    url = "https://api.kraken.com/0/public/Ticker"
184|    try:
185|        r = requests.get(url, params={"pair": kraken_symbol}, timeout=10)
186|        data = r.json()
187|        if data.get("error") and data["error"]:
188|            return None
189|        result = data.get("result", {})
190|        if not result:
191|            return None
192|        key = list(result.keys())[0]
193|        return float(result[key]["c"][0])
194|    except Exception as e:
195|        log.error(f"Erreur prix {symbole} : {e}")
196|        return None
197|
198|def get_klines(symbole, limite=100):
199|    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
200|    url = "https://api.kraken.com/0/public/OHLC"
201|    params = {"pair": kraken_symbol, "interval": 60}
202|    try:
203|        r = requests.get(url, params=params, timeout=15)
204|        data = r.json()
205|        errors = data.get("error", [])
206|        if errors:
207|            return None
208|        result = data.get("result", {})
209|        keys = [k for k in result.keys() if k != "last"]
210|        if not keys:
211|            return None
212|        candles = result[keys[0]]
213|        df = pd.DataFrame(candles, columns=['time','open','high','low','close','vwap','volume','count'])
214|        df = df.astype({'high': float, 'low': float, 'close': float, 'volume': float})
215|        return df.tail(limite).reset_index(drop=True)
216|    except Exception as e:
217|        log.error(f"Erreur klines {symbole} : {e}")
218|        return None
219|
220|def get_tendance_btc():
221|    try:
222|        kraken_symbol = "XXBTZUSD"
223|        url = "https://api.kraken.com/0/public/OHLC"
224|        r = requests.get(url, params={"pair": kraken_symbol, "interval": 15}, timeout=10)
225|        data = r.json()
226|        if data.get("error") and data["error"]:
227|            return "NEUTRE"
228|        result = data.get("result", {})
229|        keys = [k for k in result.keys() if k != "last"]
230|        if not keys:
231|            return "NEUTRE"
232|        candles = result[keys[0]]
233|        df = pd.DataFrame(candles, columns=['time','open','high','low','close','vwap','volume','count'])
234|        df = df.astype({'close': float})
235|        df = df.tail(10).reset_index(drop=True)
236|        prix_actuel = float(df['close'].iloc[-1])
237|        prix_30m_avant = float(df['close'].iloc[-3])
238|        variation = (prix_actuel - prix_30m_avant) / prix_30m_avant * 100
239|        if variation > 1.0:
240|            return "HAUSSE"
241|        elif variation < -1.0:
242|            return "BAISSE"
243|        else:
244|            return "NEUTRE"
245|    except:
246|        return "NEUTRE"
247|
248|def calculer_adx(df, periode=14):
249|    try:
250|        ind = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=periode)
251|        val = ind.adx().iloc[-1]
252|        return round(float(val), 2) if not pd.isna(val) else 0
253|    except:
254|        return 0
255|
256|def calculer_atr(df, periode=14):
257|    try:
258|        ind = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=periode)
259|        val = ind.average_true_range().iloc[-1]
260|        return round(float(val), 8) if not pd.isna(val) else 0
261|    except:
262|        return 0
263|
264|def calculer_rsi(df, periode=14):
265|    try:
266|        ind = RSIIndicator(close=df['close'], window=periode)
267|        val = ind.rsi().iloc[-1]
268|        return round(float(val), 2) if not pd.isna(val) else 50
269|    except:
270|        return 50
271|
272|def verifier_volume(df):
273|    volumes = df['volume'].tolist()
274|    if len(volumes) < 10:
275|        return True, 0
276|    moyenne_24h = sum(volumes[-24:]) / len(volumes[-24:])
277|    volume_recent = volumes[-1]
278|    ratio = volume_recent / moyenne_24h if moyenne_24h > 0 else 0
279|    return ratio >= VOLUME_MINI, round(ratio * 100, 1)
280|
281|def analyser_marche(symbole):
282|    df = get_klines(symbole, limite=100)
283|    if df is None or len(df) < 30:
284|        log.warning(f"  {symbole} : données insuffisantes")
285|        return "NEUTRE", {}
286|    adx = calculer_adx(df)
287|    atr = calculer_atr(df)
288|    rsi = calculer_rsi(df)
289|    # [FIX #3] Garde ATR : si ATR == 0 (échec de l'indicateur), le stop
290|    # collerait au prix d'entrée et déclencherait immédiatement → on skip.
291|    if atr <= 0:
292|        log.warning(f"  {symbole} : ATR invalide ({atr}) → skip")
293|        return "NEUTRE", {}
294|    volume_ok, volume_ratio = verifier_volume(df)
295|    if not volume_ok:
296|        log.info(f"  {symbole} : Volume {volume_ratio}% < {VOLUME_MINI*100}% → skip")
297|        return "NEUTRE", {}
298|    prix = df['close'].iloc[-1]
299|    atr_pct = (atr / prix) * 100
300|    if adx > ADX_MAX:
301|        log.info(f"  {symbole} : ADX {adx} > {ADX_MAX} → skip")
302|        return "NEUTRE", {}
303|    details = {"adx": adx, "atr": atr, "rsi": rsi, "atr_pct": atr_pct, "volume_ratio": volume_ratio, "df": df}
304|    rsi_achat = regime_actuel["rsi_achat"]
305|    rsi_vente = regime_actuel["rsi_vente"]
306|    if rsi < rsi_achat:
307|        log.info(f"  {symbole} : RSI {rsi} < {rsi_achat} → SURVENDU → ACHAT ✅ [Mode {regime_actuel['mode']}]")
308|        return "ACHAT", details
309|    elif rsi > rsi_vente:
310|        log.info(f"  {symbole} : RSI {rsi} > {rsi_vente} → SURACHETÉ → VENTE ✅ [Mode {regime_actuel['mode']}]")
311|        return "VENTE", details
312|    else:
313|        log.info(f"  {symbole} : RSI {rsi} | ADX {adx} → pas de signal")
314|        return "NEUTRE", details
315|
316|def choisir_meilleur_marche():
317|    log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan V8 — {len(MARCHES)} marchés [Mode {regime_actuel['mode']} | Mise {regime_actuel['mise_pct']*100}% | RSI {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}]")
318|    signaux = {}
319|    for marche in MARCHES:
320|        direction, details = analyser_marche(marche)
321|        if direction != "NEUTRE":
322|            signaux[marche] = {"direction": direction, "details": details}
323|        time.sleep(0.5)
324|    if not signaux:
325|        log.info("  => Aucun signal. On attend...")
326|        return None, "NEUTRE", {}
327|    tendance_btc = get_tendance_btc()
328|    signaux_filtres = {}
329|    for marche, data in signaux.items():
330|        direction = data["direction"]
331|        if tendance_btc == "HAUSSE" and direction == "VENTE":
332|            continue
333|        if tendance_btc == "BAISSE" and direction == "ACHAT":
334|            continue
335|        signaux_filtres[marche] = data
336|    if not signaux_filtres:
337|        log.info("  => Aucun signal après filtrage BTC. On attend...")
338|        return None, "NEUTRE", {}
339|    meilleur = max(signaux_filtres.items(), key=lambda x: (abs(x[1]["details"].get("rsi", 50) - 50), x[1]["details"].get("atr_pct", 0)))[0]
340|    direction = signaux_filtres[meilleur]["direction"]
341|    rsi = signaux_filtres[meilleur]["details"].get("rsi", 50)
342|    adx = signaux_filtres[meilleur]["details"].get("adx", 0)
343|    atr_pct = signaux_filtres[meilleur]["details"].get("atr_pct", 0)
344|    log.info(f"\n  => MEILLEUR SIGNAL : {meilleur} ({direction})\n     RSI {rsi} | ADX {adx} | ATR {round(atr_pct,2)}%")
345|    return meilleur, direction, signaux_filtres[meilleur]["details"]
346|
347|def calculer_mise(capital, nb_trades, win_rate, avg_win_pct, avg_loss_pct):
348|    mise_pct = regime_actuel["mise_pct"]
349|    if nb_trades < MIN_TRADES_KELLY:
350|        mise = capital * mise_pct
351|    else:
352|        if avg_loss_pct <= 0:
353|            mise = capital * mise_pct
354|        else:
355|            b = avg_win_pct / avg_loss_pct
356|            p = win_rate
357|            q = 1 - p
358|            kelly_full = (p * b - q) / b
359|            kelly_frac = max(0, min(kelly_full * KELLY_FRACTION, KELLY_CAP))
360|            mise = capital * kelly_frac
361|    mise = max(mise, 5.0)
362|    mise = min(mise, capital * 0.30)
363|    return round(mise, 2)
364|
365|def simuler_trade(symbole, direction, numero_trade, capital, details, etat):
366|    prix_entree = get_prix_actuel(symbole)
367|    if prix_entree is None:
368|        return "ERREUR", 0, 0, {}
369|    atr = details.get("atr", 0)
370|    if direction == "ACHAT":
371|        stop_loss = round(prix_entree - (atr * ATR_MULTIPLIER), 8)
372|        objectif_partiel = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
373|        objectif_final = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_RR), 8)
374|    else:
375|        stop_loss = round(prix_entree + (atr * ATR_MULTIPLIER), 8)
376|        objectif_partiel = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
377|        objectif_final = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_RR), 8)
378|    distance_stop_pct = (abs(prix_entree - stop_loss) / prix_entree) * 100
379|    # [FIX #4] nb_trades est déjà incrémenté AVANT l'appel à simuler_trade.
380|    # On utilise donc trades_termines = nb_trades - 1 pour le win_rate et
381|    # pour décider si Kelly est actif (sinon biais d'1 trade systématique).
382|    trades_termines = max(etat["nb_trades"] - 1, 0)
383|    win_rate = etat["nb_wins"] / trades_termines if trades_termines > 0 else 0.50
384|    avg_win = etat["avg_win_pct"] if etat["avg_win_pct"] > 0 else distance_stop_pct * RATIO_RR
385|    avg_loss = etat["avg_loss_pct"] if etat["avg_loss_pct"] > 0 else distance_stop_pct
386|    mise = calculer_mise(capital, trades_termines, win_rate, avg_win, avg_loss)
387|    log.info(f"\n  {'='*50}\n  TRADE #{numero_trade} [V8-{regime_actuel['mode']}] — {datetime.now().strftime('%H:%M:%S')}\n  {'='*50}")
388|    log.info(f"  Symbole : {symbole} ({direction})\n  Régime V8 : {regime_actuel['mode']}\n  RSI : {details.get('rsi', 0)}\n  Prix entree : {prix_entree}")
389|    log.info(f"  Stop ATR×{ATR_MULTIPLIER} : {stop_loss} ({round(distance_stop_pct,2)}%)\n  Objectif partiel : {objectif_partiel}\n  Objectif final : {objectif_final}\n  Mise : {mise}EUR ({regime_actuel['mise_pct']*100}%) | Levier x{LEVIER}\n")
390|    telegram(f"📊 <b>TRADE #{numero_trade} OUVERT [V8]</b>\n{'🟢 ACHAT' if direction=='ACHAT' else '🔴 VENTE'} {symbole}\nMode: {regime_actuel['mode']}\nRSI: {details.get('rsi',0)}\nPrix: {prix_entree}\nStop: {stop_loss} ({round(distance_stop_pct,2)}%)\nObjectif: {objectif_final}\nMise: {mise}€ × x{LEVIER}")
391|    debut = time.time()
392|    stop_actuel = stop_loss
393|    meilleur_prix = prix_entree
394|    dernier_log = 0
395|    prix_sortie = prix_entree
396|    partiel_execute = False
397|    gain_partiel = 0
398|    niveau_actuel = 2.50
399|    while True:
400|        time.sleep(CHECK_INTERVAL)
401|        prix_actuel = get_prix_actuel(symbole)
402|        if prix_actuel is None:
403|            continue
404|        prix_sortie = prix_actuel
405|        if direction == "ACHAT":
406|            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
407|        else:
408|            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)
409|        multiplicateur = get_multiplicateur_atr(pnl)
410|        distance_trailing = atr * multiplicateur
411|        stop_modifie = False
412|        if direction == "ACHAT":
413|            if prix_actuel > meilleur_prix:
414|                meilleur_prix = prix_actuel
415|            nouveau_stop = round(meilleur_prix - distance_trailing, 8)
416|            if nouveau_stop > stop_actuel:
417|                stop_actuel = nouveau_stop
418|                stop_modifie = True
419|        else:
420|            if prix_actuel < meilleur_prix:
421|                meilleur_prix = prix_actuel
422|            nouveau_stop = round(meilleur_prix + distance_trailing, 8)
423|            if nouveau_stop < stop_actuel:
424|                stop_actuel = nouveau_stop
425|                stop_modifie = True
426|        if multiplicateur != niveau_actuel and stop_modifie:
427|            if direction == "ACHAT":
428|                gain_protege = round((stop_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
429|            else:
430|                gain_protege = round((prix_entree - stop_actuel) / prix_entree * mise * LEVIER, 2)
431|            log.info(f"  [TRAILING] PnL {'+' if pnl>=0 else ''}{pnl}€ → ATR×{multiplicateur} | Stop : {stop_actuel} | Protège : ~{gain_protege}€")
432|            niveau_actuel = multiplicateur
433|        if direction == "ACHAT":
434|            atteint_partiel = not partiel_execute and prix_actuel >= objectif_partiel
435|            atteint_final = prix_actuel >= objectif_final
436|            atteint_stop = prix_actuel <= stop_actuel
437|        else:
438|            atteint_partiel = not partiel_execute and prix_actuel <= objectif_partiel
439|            atteint_final = prix_actuel <= objectif_final
440|            atteint_stop = prix_actuel >= stop_actuel
441|        duree = int((time.time() - debut) / 60)
442|        if time.time() - dernier_log >= 60:
443|            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole}: {prix_actuel} | PnL: {'+' if pnl>=0 else ''}{pnl}EUR | Stop: {stop_actuel} (ATR×{multiplicateur}) | {duree}min{' | PARTIEL ✅' if partiel_execute else ''}")
444|            dernier_log = time.time()
445|        trade_info = {"prix_entree": prix_entree, "prix_sortie": prix_sortie, "stop_loss": stop_loss, "objectif": objectif_final, "duree_minutes": duree}
446|        if atteint_partiel:
447|            gain_partiel = round(pnl * 0.5, 2)
448|            partiel_execute = True
449|            log.info(f"  SORTIE PARTIELLE 50% ! +{gain_partiel}EUR ✅")
450|            telegram(f"⚡ <b>SORTIE PARTIELLE</b>\n{symbole} | +{gain_partiel}€ sécurisés")
451|            continue
452|        if atteint_final:
453|            gain_final = round(pnl * 0.5, 2) if partiel_execute else pnl
454|            gain_total = round(gain_partiel + gain_final, 2)
455|            log.info(f"\n  OBJECTIF FINAL ! Total: +{gain_total}EUR 🎉")
456|            telegram(f"🎯 <b>OBJECTIF ATTEINT !</b>\n{symbole} {direction}\nGain : <b>+{gain_total}€</b>\nDurée : {duree} min")
457|            return "GAGNE", gain_total, mise, trade_info
458|        if atteint_stop:
459|            if partiel_execute:
460|                gain_reste = round(pnl * 0.5, 2)
461|                gain_total = round(gain_partiel + gain_reste, 2)
462|                resultat = "GAGNE" if gain_total > 0 else "PERDU"
463|                log.info(f"\n  STOP (après partiel) — {'+' if gain_total>=0 else ''}{gain_total}EUR")
464|                telegram(f"🛑 <b>STOP (après partiel)</b>\n{symbole} | {'+' if gain_total>=0 else ''}{gain_total}€\nDurée : {duree} min")
465|                return resultat, gain_total, mise, trade_info
466|            else:
467|                log.info(f"\n  STOP-LOSS ! {pnl}EUR")
468|                telegram(f"🛑 <b>STOP-LOSS</b>\n{symbole} {direction}\nPerte : <b>{pnl}€</b>\nDurée : {duree} min")
469|                return "PERDU", pnl, mise, trade_info
470|        if time.time() - debut >= TIMEOUT_TRADE:
471|            if partiel_execute:
472|                gain_reste = round(pnl * 0.5, 2)
473|                gain_total = round(gain_partiel + gain_reste, 2)
474|            else:
475|                gain_total = pnl
476|            resultat = "GAGNE" if gain_total > 0 else "PERDU"
477|            log.info(f"\n  TIMEOUT — {'+' if gain_total>=0 else ''}{gain_total}EUR")
478|            telegram(f"⏱ <b>TIMEOUT</b>\n{symbole} | {'+' if gain_total>=0 else ''}{gain_total}€\nDurée : {duree} min")
479|            return resultat, gain_total, mise, trade_info
480|
481|def verifier_kill_switch(etat, capital):
482|    if capital < CAPITAL_INITIAL * SEUIL_RUINE:
483|        log.critical(f"SEUIL DE RUINE ! Capital {capital}EUR")
484|        telegram(f"🚨 <b>SEUIL DE RUINE !</b>\nCapital : {capital}€\nBot arrêté !")
485|        return "RUINE"
486|    pause_until = etat.get("pause_until", 0)
487|    if time.time() < pause_until:
488|        restant = int((pause_until - time.time()) / 60)
489|        log.info(f"  En pause — {restant} minutes restantes")
490|        time.sleep(60)
491|        return "PAUSE"
492|    else:
493|        # [FIX #1] On ne réinitialise les pertes consécutives QUE si on
494|        # sortait réellement d'une pause (pause_until > 0). Sinon, à
495|        # l'état initial (pause_until == 0), la branche else réinitialisait
496|        # à tort pertes_consecutives à 0 AVANT que le kill switch puisse
497|        # se déclencher → kill switch jamais activé.
498|        if pause_until > 0 and etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:
499|            log.info("  Fin de la pause → réinitialisation des pertes consécutives à 0")
500|            etat["pertes_consecutives"] = 0
501|            etat["pause_until"] = 0
502|            sauvegarder_etat(etat)
503|    if etat["pertes_consecutives"] >= MAX_PERTES_CONSECUTIVES:
504|        log.warning(f"KILL SWITCH — {MAX_PERTES_CONSECUTIVES} pertes consecutives !")
505|        telegram(f"⚠️ <b>KILL SWITCH</b>\n{MAX_PERTES_CONSECUTIVES} pertes consécutives\nPause 12h")
506|        etat["pause_until"] = int(time.time()) + PAUSE_DUREE
507|        etat["pertes_consecutives"] = 0
508|        sauvegarder_etat(etat)
509|        return "PAUSE"
510|    return "OK"
511|
512|def afficher_tableau_de_bord(etat):
513|    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
514|    perf = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
515|    log.info(f"\n  {'='*55}\n  BOT MEAN REVERSION V8 — TABLEAU DE BORD\n  {'='*55}")
516|    log.info(f"  Capital actuel : {round(etat['capital'],2)}EUR ({'+' if perf>=0 else ''}{round(perf,2)}%)")
517|    log.info(f"  Trades total   : {etat['nb_trades']}")
518|    log.info(f"  Victoires      : {etat['nb_wins']} ({win_rate:.1f}%)")
519|    log.info(f"  Defaites       : {etat['nb_losses']}")
520|    log.info(f"  Pertes consec. : {etat['pertes_consecutives']}/{MAX_PERTES_CONSECUTIVES}")
521|    log.info(f"  Kelly actif    : {'Non (<30 trades)' if etat['nb_trades'] < MIN_TRADES_KELLY else 'Oui'}")
522|    log.info(f"  Total gagne    : +{round(etat['total_gagne'],2)}EUR")
523|    log.info(f"  Total perdu    : -{round(etat['total_perdu'],2)}EUR")
524|    log.info(f"  BENEFICE NET   : {'+' if etat['cumul_net']>=0 else ''}{round(etat['cumul_net'],2)}EUR")
525|    log.info(f"  [V8] Régime    : {regime_actuel['mode']} | Mise {regime_actuel['mise_pct']*100}% | RSI {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}")
526|    if etat.get("historique"):
527|        log.info(f"\n  Derniers trades :")
528|        for h in etat["historique"][-5:]:
529|            icone = "OK" if h["resultat"] == "GAGNE" else "XX"
530|            log.info(f"    [{icone}] {h['heure']} | {h['marche']} | {h['direction']} | {'+' if h['gain']>=0 else ''}{h['gain']}EUR | Capital: {h['capital']}EUR")
531|    log.info(f"  {'='*55}")
532|
533|def envoyer_rapport_telegram(etat):
534|    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
535|    perf = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
536|    telegram(f"📈 <b>RAPPORT BOT V8</b>\nCapital : <b>{round(etat['capital'],2)}€</b> ({'+' if perf>=0 else ''}{round(perf,2)}%)\nTrades : {etat['nb_trades']} | WR : {round(win_rate,1)}%\nGagné : +{round(etat['total_gagne'],2)}€\nPerdu : -{round(etat['total_perdu'],2)}€\n<b>NET : {'+' if etat['cumul_net']>=0 else ''}{round(etat['cumul_net'],2)}€</b>\nMode V8 : {regime_actuel['mode']}")
537|
538|def demarrer_bot():
539|    log.info("=" * 55)
540|    log.info("  BOT MEAN REVERSION V8 — ADAPTATIF (TRAILING CONTINU)")
541|    log.info(f"  Capital : {CAPITAL_INITIAL}EUR | Levier x{LEVIER} | Scan régime toutes les {V8_SCAN_INTERVAL//60} minutes")
542|    log.info(f"  Protection trailing stop dès +0.75€ | Telegram : {'✅ ON' if TELEGRAM_TOKEN else '❌ OFF'}")
543|    log.info("=" * 55)
544|    init_database()
545|    etat = charger_etat()
546|    detecter_regime()
547|    afficher_tableau_de_bord(etat)
548|    telegram(f"🚀 <b>BOT V8 DÉMARRÉ</b>\nCapital : {round(etat['capital'],2)}€\nRégime initial : {regime_actuel['mode']}\nMise {regime_actuel['mise_pct']*100}% | RSI {regime_actuel['rsi_achat']}/{regime_actuel['rsi_vente']}\nTrailing stop continu (protection dès +0.75€)\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
549|    while True:
550|        try:
551|            if time.time() - regime_actuel["derniere_maj"] >= V8_SCAN_INTERVAL:
552|                log.info("  [V8] Mise à jour du régime de marché...")
553|                detecter_regime()
554|            statut = verifier_kill_switch(etat, etat["capital"])
555|            if statut == "RUINE":
556|                break
557|            if statut == "PAUSE":
558|                etat = charger_etat()
559|                continue
560|            symbole, direction, details = choisir_meilleur_marche()
561|            if direction == "NEUTRE" or symbole is None:
562|                etat["nb_skips"] += 1
563|                sauvegarder_etat(etat)
564|                log.info(f"  Nouvelle analyse dans 2 minutes...")
565|                time.sleep(PAUSE)
566|                continue
567|            etat["nb_trades"] += 1
568|            resultat, gain, mise, trade_info = simuler_trade(symbole, direction, etat["nb_trades"], etat["capital"], details, etat)
569|            if resultat == "ERREUR":
570|                etat["nb_trades"] -= 1
571|                time.sleep(PAUSE)
572|                continue
573|            etat["capital"] = round(etat["capital"] + gain, 2)
574|            etat["cumul_net"] = round(etat["capital"] - CAPITAL_INITIAL, 2)
575|            if resultat == "GAGNE":
576|                etat["nb_wins"] += 1
577|                etat["total_gagne"] = round(etat["total_gagne"] + gain, 2)
578|                etat["pertes_consecutives"] = 0
579|                gain_pct = (gain / max(mise * LEVIER, 1)) * 100
580|                if etat["avg_win_pct"] == 0:
581|                    etat["avg_win_pct"] = gain_pct
582|                else:
583|                    etat["avg_win_pct"] = round((etat["avg_win_pct"] * (etat["nb_wins"]-1) + gain_pct) / etat["nb_wins"], 4)
584|            else:
585|                etat["nb_losses"] += 1
586|                etat["total_perdu"] = round(etat["total_perdu"] + abs(gain), 2)
587|                etat["pertes_consecutives"] += 1
588|                perte_pct = (abs(gain) / max(mise * LEVIER, 1)) * 100
589|                if etat["avg_loss_pct"] == 0:
590|                    etat["avg_loss_pct"] = perte_pct
591|                else:
592|                    etat["avg_loss_pct"] = round((etat["avg_loss_pct"] * (etat["nb_losses"]-1) + perte_pct) / etat["nb_losses"], 4)
593|            enregistrer_trade({
594|                'marche': symbole, 'direction': direction, 'resultat': resultat,
595|                'prix_entree': trade_info['prix_entree'], 'prix_sortie': trade_info['prix_sortie'],
596|                'stop_loss': trade_info['stop_loss'], 'objectif': trade_info['objectif'],
597|                'mise': mise, 'gain': round(gain, 2), 'capital_apres': etat['capital'],
598|                'duree_minutes': trade_info['duree_minutes'], 'score': None,
599|                'adx': details.get('adx'), 'atr': details.get('atr'), 'rsi': details.get('rsi')
600|            })
601|            # [FIX #2] L'ajout à l'historique doit se faire AVANT sauvegarder_etat()
602|            # sinon la dernière entrée n'est jamais persistée et serait perdue
603|            # en cas de crash ou redémarrage du bot.
604|            etat['historique'].append({
605|                'heure': datetime.now().strftime('%Y-%m-%d %H:%M'), 'marche': symbole,
606|                'direction': direction, 'resultat': resultat, 'gain': round(gain, 2),
607|                'mise': round(mise, 2), 'capital': etat['capital']
608|            })
609|            sauvegarder_etat(etat)
610|            afficher_tableau_de_bord(etat)
611|            envoyer_rapport_telegram(etat)
612|            log.info(f"  Pause 2 minutes avant prochain trade...")
613|            time.sleep(PAUSE)
614|        except KeyboardInterrupt:
615|            log.info("Bot arrete.")
616|            break
617|        except Exception as e:
618|            log.error(f"Erreur : {e}")
619|            time.sleep(60)
620|
621|if __name__ == "__main__":
622|    demarrer_bot()
623|
[End of file]
