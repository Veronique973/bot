"""
Gestion PostgreSQL — Bot Trading
Compatible Bot 1 V7.3 et Bot 3 V8
"""

import pg8000
import os
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL', '')

def get_connection():
    try:
        import re
        url = DATABASE_URL
        # Parse DATABASE_URL
        pattern = r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)'
        match = re.match(pattern, url)
        if not match:
            raise Exception(f"URL invalide : {url}")
        user, password, host, port, database = match.groups()
        conn = pg8000.connect(
            host=host,
            port=int(port),
            database=database,
            user=user,
            password=password,
            ssl_context=True
        )
        return conn
    except Exception as e:
        log.error(f"Erreur connexion PostgreSQL : {e}")
        return None

def init_database():
    conn = get_connection()
    if conn is None:
        log.warning("PostgreSQL non disponible — mode JSON")
        return
    try:
        cursor = conn.cursor()

        # Table état du bot
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                capital REAL DEFAULT 215.0,
                cumul_net REAL DEFAULT 0.0,
                total_gagne REAL DEFAULT 0.0,
                total_perdu REAL DEFAULT 0.0,
                nb_trades INTEGER DEFAULT 0,
                nb_wins INTEGER DEFAULT 0,
                nb_losses INTEGER DEFAULT 0,
                nb_skips INTEGER DEFAULT 0,
                pertes_consecutives INTEGER DEFAULT 0,
                avg_win_pct REAL DEFAULT 0.0,
                avg_loss_pct REAL DEFAULT 0.0,
                pause_until INTEGER DEFAULT 0,
                historique TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table historique des trades
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_history (
                id SERIAL PRIMARY KEY,
                marche VARCHAR(20),
                direction VARCHAR(10),
                resultat VARCHAR(10),
                prix_entree REAL,
                prix_sortie REAL,
                stop_loss REAL,
                objectif REAL,
                mise REAL,
                gain REAL,
                capital_apres REAL,
                duree_minutes INTEGER,
                score REAL,
                adx REAL,
                atr REAL,
                rsi REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Insérer l'état initial si pas encore présent
        cursor.execute('SELECT COUNT(*) FROM bot_state')
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute('INSERT INTO bot_state (id) VALUES (1)')

        conn.commit()
        cursor.close()
        conn.close()
        log.info("Base PostgreSQL initialisee")
    except Exception as e:
        log.error(f"Erreur init database : {e}")

def charger_etat():
    conn = get_connection()
    if conn is None:
        return _etat_defaut()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM bot_state WHERE id = 1')
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row is None:
            return _etat_defaut()

        colonnes = [
            'id', 'capital', 'cumul_net', 'total_gagne', 'total_perdu',
            'nb_trades', 'nb_wins', 'nb_losses', 'nb_skips',
            'pertes_consecutives', 'avg_win_pct', 'avg_loss_pct',
            'pause_until', 'historique', 'updated_at'
        ]
        etat = dict(zip(colonnes, row))
        etat['historique'] = json.loads(etat.get('historique', '[]'))
        return etat

    except Exception as e:
        log.error(f"Erreur chargement état : {e}")
        return _etat_defaut()

def sauvegarder_etat(etat):
    conn = get_connection()
    if conn is None:
        return
    try:
        cursor = conn.cursor()
        historique_json = json.dumps(etat.get('historique', [])[-20:], ensure_ascii=False)
        cursor.execute('''
            UPDATE bot_state SET
                capital = %s,
                cumul_net = %s,
                total_gagne = %s,
                total_perdu = %s,
                nb_trades = %s,
                nb_wins = %s,
                nb_losses = %s,
                nb_skips = %s,
                pertes_consecutives = %s,
                avg_win_pct = %s,
                avg_loss_pct = %s,
                pause_until = %s,
                historique = %s,
                updated_at = %s
            WHERE id = 1
        ''', (
            etat['capital'], etat['cumul_net'],
            etat['total_gagne'], etat['total_perdu'],
            etat['nb_trades'], etat['nb_wins'],
            etat['nb_losses'], etat.get('nb_skips', 0),
            etat['pertes_consecutives'],
            etat.get('avg_win_pct', 0), etat.get('avg_loss_pct', 0),
            etat.get('pause_until', 0),
            historique_json,
            datetime.now()
        ))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        log.error(f"Erreur sauvegarde état : {e}")

def enregistrer_trade(trade):
    conn = get_connection()
    if conn is None:
        return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trade_history (
                marche, direction, resultat,
                prix_entree, prix_sortie, stop_loss, objectif,
                mise, gain, capital_apres, duree_minutes,
                score, adx, atr, rsi
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            trade.get('marche'), trade.get('direction'), trade.get('resultat'),
            trade.get('prix_entree'), trade.get('prix_sortie'),
            trade.get('stop_loss'), trade.get('objectif'),
            trade.get('mise'), trade.get('gain'),
            trade.get('capital_apres'), trade.get('duree_minutes'),
            trade.get('score'), trade.get('adx'),
            trade.get('atr'), trade.get('rsi')
        ))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        log.error(f"Erreur enregistrement trade : {e}")

def _etat_defaut():
    return {
        'capital': 215.0,
        'cumul_net': 0.0,
        'total_gagne': 0.0,
        'total_perdu': 0.0,
        'nb_trades': 0,
        'nb_wins': 0,
        'nb_losses': 0,
        'nb_skips': 0,
        'pertes_consecutives': 0,
        'avg_win_pct': 0.0,
        'avg_loss_pct': 0.0,
        'pause_until': 0,
        'historique': []
    }
