#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de gestion de portefeuille pour la simulation Parquet CAC 40
================================================================
Stratégie factorielle systématique :
- Momentum Cross-Sectional (sur ~20 relevés)
- Pondération Inverse-Volatilité (Risk Parity simplifiée)
- Plafonds de risque : max 15% par ligne, max 30% par secteur
- Stop-loss mécanique à -10% du PRU
- Coussin de liquidité permanent : minimum 5% en cash
- Filtre de rebalancement (seuil d'écart minimal de 3% pour limiter les frais de transaction)
- Exécution automatique synchronisée avec les 10 relevés quotidiens de marché
"""

import os
import sys
import time
import math
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from dotenv import load_dotenv
import pandas as pd
import numpy as np

try:
    from supabase import create_client, Client
except ImportError:
    print("[CRITICAL] La bibliothèque supabase n'est pas installée. Exécutez : pip install -r requirements.txt")
    sys.exit(1)


# =====================================================================
# Configuration du Logging
# =====================================================================
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("ParquetBot")
    logger.setLevel(logging.INFO)

    # Éviter les doublons de handlers lors d'exécutions répétées
    if logger.hasHandlers():
        logger.handlers.clear()

    # Format de log détaillé
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Rotating File Handler (max 10 Mo par fichier, 5 backups)
    log_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(log_dir, "parquet_bot.log")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()


# =====================================================================
# Configuration Globale & Chargement de l'environnement
# =====================================================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://tsztojpbwiwlluheoyos.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_UoPH4BSZJkSd9f_YBxzPpg_2ZopR2OA")

ETUDIANT_EMAIL = os.getenv("ETUDIANT_EMAIL", "")
ETUDIANT_PASSWORD = os.getenv("ETUDIANT_PASSWORD", "")

DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() in ("true", "1", "yes", "t")

# Paramètres Stratégie
LOOKBACK_PERIOD = int(os.getenv("LOOKBACK_PERIOD", "20"))
MAX_STOCK_WEIGHT = float(os.getenv("MAX_STOCK_WEIGHT", "0.15"))
MAX_SECTOR_WEIGHT = float(os.getenv("MAX_SECTOR_WEIGHT", "0.30"))
MIN_CASH_BUFFER = float(os.getenv("MIN_CASH_BUFFER", "0.05"))
STOP_LOSS_THRESHOLD = float(os.getenv("STOP_LOSS_THRESHOLD", "0.10"))
REBALANCE_THRESHOLD = float(os.getenv("REBALANCE_THRESHOLD", "0.03"))

# Heures fixes officielles des 10 relevés de marché (heure de Paris)
FIXED_MARKET_HOURS = [
    (9, 0), (10, 0), (11, 0), (12, 0), (13, 0),
    (14, 0), (15, 0), (16, 0), (17, 0), (17, 30)
]
DELAY_BUFFER_MINUTES = 22  # Le relevé est disponible vers H+15 à H+20 avec le délai Yahoo Finance


class ParquetCAC40Bot:
    def __init__(self):
        self.supabase: Optional[Client] = None
        self.session = None
        self.etudiant_id: Optional[str] = None
        self._init_supabase()

    def _init_supabase(self):
        """Initialise le client Supabase et authentifie l'utilisateur."""
        if not SUPABASE_URL or not SUPABASE_KEY:
            logger.critical("SUPABASE_URL ou SUPABASE_KEY non renseigné dans l'environnement !")
            raise ValueError("Identifiants Supabase manquants.")

        logger.info(f"Connexion au backend Supabase : {SUPABASE_URL}")
        self.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        if not ETUDIANT_EMAIL or not ETUDIANT_PASSWORD:
            logger.warning(
                "ETUDIANT_EMAIL ou ETUDIANT_PASSWORD manquant dans le fichier .env ! "
                "Le bot fonctionnera en mode lecture publique sans authentification RLS."
            )
            return

        try:
            logger.info(f"Authentification de l'étudiant : {ETUDIANT_EMAIL}")
            auth_response = self.supabase.auth.sign_in_with_password({
                "email": ETUDIANT_EMAIL,
                "password": ETUDIANT_PASSWORD
            })
            self.session = auth_response.session
            logger.info("Authentification Supabase réussie avec succès.")
            
            # Vérifier l'identifiant étudiant via RPC
            self._fetch_etudiant_identity()
        except Exception as e:
            logger.error(f"Erreur lors de l'authentification Supabase : {e}")

    def _fetch_etudiant_identity(self):
        """Récupère l'identifiant de l'étudiant via la fonction RPC mon_etudiant."""
        try:
            res = self.supabase.rpc("mon_etudiant").execute()
            if res.data:
                self.etudiant_id = str(res.data)
                logger.info(f"Identifiant étudiant (mon_etudiant) : {self.etudiant_id}")
        except Exception as e:
            logger.debug(f"Impossible d'appeler mon_etudiant (peut être normal selon les permissions) : {e}")

    # =================================================================
    # Collecte des données de marché et de portefeuille
    # =================================================================
    def fetch_univers(self) -> pd.DataFrame:
        """Récupère la liste des titres actifs du CAC 40."""
        try:
            query = self.supabase.table("titres").select("*")
            res = query.execute()
            df = pd.DataFrame(res.data)
            if df.empty:
                logger.warning("Table 'titres' vide ou inaccessible.")
                return pd.DataFrame()
            # Filtrer les indices (ex: ^FCHI) et conserver uniquement les actions actives
            if "actif" in df.columns:
                df = df[df["actif"] == True]
            if "est_indice" in df.columns:
                df = df[df["est_indice"] != True]
            df = df[~df["symbole"].str.startswith("^")]
            return df
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des titres : {e}")
            return pd.DataFrame()

    def fetch_cours_history(self) -> pd.DataFrame:
        """Récupère l'historique récent des cours pour toutes les valeurs."""
        try:
            # Récupérer un nombre suffisant de lignes pour calculer momentum et volatilité
            query = (
                self.supabase.table("cours")
                .select("*")
                .order("horodatage", desc=True)
                .limit(2500)
            )
            res = query.execute()
            df = pd.DataFrame(res.data)
            if df.empty:
                logger.warning("Table 'cours' vide.")
                return pd.DataFrame()
            df["horodatage"] = pd.to_datetime(df["horodatage"])
            # Support du nom de colonne 'prix' (ou fallback 'valeur')
            price_col = "prix" if "prix" in df.columns else ("valeur" if "valeur" in df.columns else None)
            if price_col:
                df["valeur"] = pd.to_numeric(df[price_col], errors="coerce")
            else:
                logger.error(f"Aucune colonne de prix trouvée dans 'cours' : {df.columns.tolist()}")
                return pd.DataFrame()
            df = df.sort_values(by=["symbole", "horodatage"], ascending=[True, True])
            return df
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des cours : {e}")
            return pd.DataFrame()

    def fetch_portefeuille(self) -> Tuple[float, float]:
        """
        Récupère le résumé du portefeuille (cash et valeur totale).
        Retourne : (solde_cash, valeur_totale)
        """
        try:
            res = self.supabase.table("v_portefeuille").select("*").execute()
            if res.data and len(res.data) > 0:
                row = res.data[0]
                solde = float(row.get("solde", 0.0) or 0.0)
                valeur_totale = float(row.get("valeur_totale", solde) or solde)
                return solde, valeur_totale
        except Exception as e:
            logger.error(f"Erreur lors de la lecture de v_portefeuille : {e}")
        return 0.0, 0.0

    def fetch_positions(self) -> pd.DataFrame:
        """Récupère la vue des positions actuellement ouvertes."""
        try:
            res = self.supabase.table("v_positions").select("*").execute()
            df = pd.DataFrame(res.data)
            if df.empty:
                return pd.DataFrame(columns=["symbole", "quantite", "dernier_cours", "pru", "valeur", "plus_value_pct"])
            for col in ["quantite", "dernier_cours", "pru", "valeur", "plus_value", "plus_value_pct"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
            return df
        except Exception as e:
            logger.error(f"Erreur lors de la lecture de v_positions : {e}")
            return pd.DataFrame()

    def fetch_ordres_en_attente(self) -> List[Dict[str, Any]]:
        """Récupère les ordres encore en attente d'exécution."""
        try:
            res = self.supabase.rpc("mes_ordres_en_attente").execute()
            return res.data or []
        except Exception as e:
            logger.debug(f"RPC mes_ordres_en_attente : {e}")
            try:
                res = self.supabase.table("ordres").select("*").eq("statut", "EN_ATTENTE").execute()
                return res.data or []
            except Exception:
                return []

    def fetch_mon_rang(self) -> Optional[Tuple[int, int]]:
        """Récupère le rang actuel au classement (rang, effectif)."""
        try:
            res = self.supabase.rpc("mon_rang").execute()
            if res.data:
                if isinstance(res.data, list) and len(res.data) > 0:
                    row = res.data[0]
                    return int(row.get("rang", 1)), int(row.get("effectif", 1))
                elif isinstance(res.data, (int, str)):
                    return int(res.data), 0
        except Exception as e:
            logger.debug(f"RPC mon_rang indisponible : {e}")
        return None

    # =================================================================
    # Calculs Quantitatifs & Stratégie de Portefeuille
    # =================================================================
    def compute_factors(self, df_cours: pd.DataFrame, df_titres: pd.DataFrame) -> pd.DataFrame:
        """
        Calcule pour chaque titre :
        1. Le Momentum cross-sectional (rendement sur les ~20 derniers relevés).
        2. La Volatilité historique (écart-type des variations relatives).
        3. Le Score de pondération (Momentum normalisé pondéré par l'inverse de la volatilité).
        """
        factors = []
        secteur_map = {}
        valid_symbols = set()
        if not df_titres.empty and "symbole" in df_titres.columns:
            valid_symbols = set(df_titres["symbole"].dropna().unique())
            if "secteur" in df_titres.columns:
                secteur_map = dict(zip(df_titres["symbole"], df_titres["secteur"].fillna("Autre")))

        for symbole, group in df_cours.groupby("symbole"):
            if valid_symbols and symbole not in valid_symbols:
                continue
            group = group.sort_values(by="horodatage")
            prices = group["valeur"].dropna().values
            if len(prices) < 5:
                # Historique trop court
                continue

            dernier_prix = prices[-1]
            lookback = min(len(prices), LOOKBACK_PERIOD)
            prix_debut = prices[-lookback]

            # Rendement Momentum
            momentum = (dernier_prix - prix_debut) / prix_debut if prix_debut > 0 else 0.0

            # Volatilité (sur les rendements pas à pas)
            returns = np.diff(prices[-lookback:]) / prices[-lookback:-1]
            volatilite = float(np.std(returns)) if len(returns) > 1 else 0.02
            if volatilite <= 1e-6:
                volatilite = 0.02

            factors.append({
                "symbole": symbole,
                "dernier_cours": dernier_prix,
                "momentum": momentum,
                "volatilite": volatilite,
                "inverse_vol": 1.0 / volatilite,
                "secteur": secteur_map.get(symbole, "Autre")
            })

        df_factors = pd.DataFrame(factors)
        if df_factors.empty:
            return pd.DataFrame()

        # Filtrer les titres à momentum positif (surperformance)
        # On sélectionne les meilleurs déciles / titres positifs
        df_positive = df_factors[df_factors["momentum"] > 0].copy()
        if df_positive.empty:
            logger.info("Aucun titre avec momentum strictement positif ; sélection des 5 meilleurs titres relatifs.")
            df_positive = df_factors.sort_values(by="momentum", ascending=False).head(5).copy()

        # Score brut = Momentum * Inverse Volatilité (Risk Parity ajustée)
        df_positive["raw_score"] = df_positive["momentum"] * df_positive["inverse_vol"]
        total_raw = df_positive["raw_score"].sum()

        if total_raw > 0:
            df_positive["target_weight"] = df_positive["raw_score"] / total_raw
        else:
            df_positive["target_weight"] = 1.0 / len(df_positive)

        # Application des contraintes de diversification (Plafonds 15% par titre, 30% par secteur)
        df_target = self._apply_portfolio_constraints(df_positive)
        return df_target

    def _apply_portfolio_constraints(self, df_alloc: pd.DataFrame) -> pd.DataFrame:
        """
        Applique les plafonds stricts :
        - Max 15% par ligne (MAX_STOCK_WEIGHT)
        - Max 30% par secteur GICS (MAX_SECTOR_WEIGHT)
        - Prise en compte du coussin de liquidité (1 - MIN_CASH_BUFFER allouable aux actions)
        """
        df = df_alloc.copy()
        max_equity_allocation = 1.0 - MIN_CASH_BUFFER

        # Normaliser pour ne pas dépasser max_equity_allocation
        df["target_weight"] = df["target_weight"] * max_equity_allocation

        # Itération pour écrêter et redistribuer l'excédent
        for _ in range(5):
            # Plafond individuel
            df["target_weight"] = df["target_weight"].clip(upper=MAX_STOCK_WEIGHT)

            # Plafond sectoriel
            for secteur, group in df.groupby("secteur"):
                secteur_sum = group["target_weight"].sum()
                if secteur_sum > MAX_SECTOR_WEIGHT:
                    scale = MAX_SECTOR_WEIGHT / secteur_sum
                    df.loc[df["secteur"] == secteur, "target_weight"] *= scale

            # Renormalisation si en dessous du budget investi
            current_total = df["target_weight"].sum()
            if current_total > max_equity_allocation:
                df["target_weight"] = df["target_weight"] * (max_equity_allocation / current_total)

        return df

    # =================================================================
    # Gestion des Risques : Stop-Loss Mécanique
    # =================================================================
    def check_stop_losses(self, df_positions: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Identifie les positions ayant touché le stop-loss mécanique (-10% sous le PRU).
        Ces positions doivent être coupées en priorité absolue.
        """
        stop_orders = []
        if df_positions.empty:
            return stop_orders

        for _, pos in df_positions.iterrows():
            symbole = pos["symbole"]
            quantite = int(pos["quantite"])
            pru = float(pos.get("pru", 0.0))
            dernier_cours = float(pos.get("dernier_cours", 0.0))
            plus_value_pct = float(pos.get("plus_value_pct", 0.0))

            if quantite <= 0 or pru <= 0:
                continue

            # Calcul de la perte
            perte_pct = (dernier_cours - pru) / pru if pru > 0 else (plus_value_pct / 100.0)

            if perte_pct <= -STOP_LOSS_THRESHOLD:
                logger.warning(
                    f"🛑 [STOP-LOSS DÉCLENCHÉ] {symbole} | PRU={pru:.2f}€ | Cours={dernier_cours:.2f}€ | Perte={perte_pct*100:.2f}% (Seuil={-STOP_LOSS_THRESHOLD*100:.1f}%)"
                )
                stop_orders.append({
                    "symbole": symbole,
                    "sens": "VENTE",
                    "quantite": quantite,
                    "raison": f"STOP_LOSS ({perte_pct*100:.2f}%)"
                })

        return stop_orders

    # =================================================================
    # Génération et Exécution des Ordres
    # =================================================================
    def compute_rebalancing_orders(
        self,
        df_target: pd.DataFrame,
        df_positions: pd.DataFrame,
        valeur_totale: float,
        solde_cash: float
    ) -> List[Dict[str, Any]]:
        """
        Calcule les ordres de rééquilibrage nécessaires en appliquant le seuil d'écart (REBALANCE_THRESHOLD).
        """
        orders = []
        positions_map = {}
        if not df_positions.empty:
            for _, row in df_positions.iterrows():
                positions_map[row["symbole"]] = {
                    "quantite": int(row["quantite"]),
                    "dernier_cours": float(row["dernier_cours"]),
                    "valeur": float(row["valeur"])
                }

        # 1. Traiter les ventes des positions qui ne sont plus dans le portefeuille cible
        target_symbols = set(df_target["symbole"].tolist()) if not df_target.empty else set()
        for symbole, pos_info in positions_map.items():
            if symbole not in target_symbols and pos_info["quantite"] > 0:
                orders.append({
                    "symbole": symbole,
                    "sens": "VENTE",
                    "quantite": pos_info["quantite"],
                    "raison": "SORTIE_DE_SELECTION"
                })

        # 2. Ajustements sur les titres de la cible
        # Calcul du plafond de cash dépensable pour préserver STRICTEMENT les 5% de liquidités
        seuil_liquidites = valeur_totale * MIN_CASH_BUFFER
        cash_disponible_apres_ventes = solde_cash
        # Estimer le cash dégagé par les ventes prévues
        for o in orders:
            if o["sens"] == "VENTE" and o["symbole"] in positions_map:
                cash_disponible_apres_ventes += o["quantite"] * positions_map[o["symbole"]]["dernier_cours"]

        # Budget maximum allouable aux achats
        budget_achats_restant = max(0.0, cash_disponible_apres_ventes - seuil_liquidites)
        logger.info(
            f"🔒 Garantie Liquidités : Seuil minimum = {seuil_liquidites:,.2f} € (5%) | Budget maximum disponible pour les achats = {budget_achats_restant:,.2f} €"
        )

        for _, target_row in df_target.iterrows():
            symbole = target_row["symbole"]
            target_weight = target_row["target_weight"]
            dernier_cours = target_row["dernier_cours"]

            if dernier_cours <= 0:
                continue

            target_valeur = valeur_totale * target_weight
            target_qty = int(math.floor(target_valeur / dernier_cours))

            current_info = positions_map.get(symbole, {"quantite": 0, "valeur": 0.0})
            current_qty = current_info["quantite"]
            current_valeur = current_qty * dernier_cours
            current_weight = current_valeur / valeur_totale if valeur_totale > 0 else 0.0

            weight_diff = target_weight - current_weight

            # Application du filtre anti-frais de transaction
            if abs(weight_diff) < REBALANCE_THRESHOLD:
                logger.debug(
                    f"Écart négligeable pour {symbole} : {weight_diff*100:+.2f}% (seuil {REBALANCE_THRESHOLD*100:.1f}%) - Aucun ordre généré."
                )
                continue

            qty_diff = target_qty - current_qty

            if qty_diff > 0:
                # Plafonner l'achat au budget cash restant pour respecter les 5% (50 000 €)
                max_qty_possible = int(math.floor(budget_achats_restant / dernier_cours))
                buy_qty = min(qty_diff, max_qty_possible)

                if buy_qty > 0:
                    cout_achat = buy_qty * dernier_cours
                    budget_achats_restant -= cout_achat
                    orders.append({
                        "symbole": symbole,
                        "sens": "ACHAT",
                        "quantite": buy_qty,
                        "cout_estime": cout_achat,
                        "raison": f"REBALANCEMENT (+{weight_diff*100:.1f}%)"
                    })
                else:
                    logger.warning(
                        f"Achat de {symbole} reporté : préservation stricte du coussin de 5% de liquidités ({seuil_liquidites:,.2f} €)."
                    )
            elif qty_diff < 0:
                orders.append({
                    "symbole": symbole,
                    "sens": "VENTE",
                    "quantite": abs(qty_diff),
                    "raison": f"REBALANCEMENT ({weight_diff*100:.1f}%)"
                })

        return orders

    def execute_order(self, sens: str, symbole: str, quantite: int, raison: str = "") -> bool:
        """
        Envoie un ordre au serveur via la RPC passer_mon_ordre.
        En mode DRY_RUN, consigne l'ordre sans l'envoyer.
        """
        if quantite <= 0:
            return False

        log_prefix = "[DRY-RUN]" if DRY_RUN else "[LIVE-TRADE]"
        logger.info(
            f"{log_prefix} Ordre : {sens} {quantite}x {symbole} | Motif : {raison}"
        )

        if DRY_RUN:
            logger.info(f"{log_prefix} Ordre simulé avec succès (DRY_RUN=true, aucune transmission réseau).")
            return True

        try:
            # Appel de la RPC passer_mon_ordre(p_sens, p_symbole, p_quantite)
            params = {
                "p_sens": sens.upper(),
                "p_symbole": symbole,
                "p_quantite": int(quantite)
            }
            res = self.supabase.rpc("passer_mon_ordre", params).execute()
            logger.info(f"✅ Ordre envoyé avec succès ! Réponse serveur : {res.data}")
            return True
        except Exception as e:
            logger.error(f"❌ Échec de transmission de l'ordre {sens} {quantite}x {symbole} : {e}")
            return False

    # =================================================================
    # Cycle de Décision Complet
    # =================================================================
    def run_strategy_cycle(self):
        """Exécute un cycle complet d'analyse et de rééquilibrage de portefeuille."""
        logger.info("=" * 60)
        logger.info(f"Démarrage du cycle de trading - Mode : {'DRY-RUN (Simulation)' if DRY_RUN else 'LIVE (Réel)'}")
        logger.info("=" * 60)

        # 1. Vérification du classement et des ordres en cours
        rang_info = self.fetch_mon_rang()
        if rang_info is not None:
            rang, effectif = rang_info
            logger.info(f"🏆 Rang actuel dans la compétition : {rang} / {effectif} étudiants")

        ordres_en_attente = self.fetch_ordres_en_attente()
        if ordres_en_attente:
            logger.info(f"⏳ {len(ordres_en_attente)} ordre(s) en attente d'exécution au prochain relevé.")

        # 2. Récupération de l'état du portefeuille
        solde_cash, valeur_totale = self.fetch_portefeuille()
        if valeur_totale <= 0:
            logger.warning("Valeur totale du portefeuille indéterminée ou nulle. Utilisation du capital par défaut (1 000 000 €).")
            valeur_totale = 1000000.0
            solde_cash = 1000000.0

        logger.info(f"💼 Portefeuille : Cash={solde_cash:,.2f} € | Valeur Totale={valeur_totale:,.2f} €")

        df_positions = self.fetch_positions()
        if not df_positions.empty:
            logger.info(f"📊 Positions ouvertes actuelles ({len(df_positions)} lignes) :")
            for _, pos in df_positions.iterrows():
                logger.info(
                    f"   - {pos['symbole']} : {pos['quantite']} actions @ {pos.get('dernier_cours', 0.0):.2f}€ (PRU: {pos.get('pru', 0.0):.2f}€, PnL: {pos.get('plus_value_pct', 0.0):+.2f}%)"
                )

        # 3. ÉTAPE PRIORITAIRE : Exécution des Stop-Loss
        stop_orders = self.check_stop_losses(df_positions)
        symbols_stopped = set()
        for order in stop_orders:
            success = self.execute_order(
                sens=order["sens"],
                symbole=order["symbole"],
                quantite=order["quantite"],
                raison=order["raison"]
            )
            if success:
                symbols_stopped.add(order["symbole"])

        # 4. Collecte des données de marché
        df_titres = self.fetch_univers()
        df_cours = self.fetch_cours_history()

        if df_cours.empty:
            logger.warning("Historique de cours indisponible pour le calcul factoriel. Fin du cycle.")
            return

        # 5. Calcul de l'allocation cible
        df_target = self.compute_factors(df_cours, df_titres)

        # Exclure les titres qui viennent d'être coupés par stop-loss pour ce cycle
        if symbols_stopped and not df_target.empty:
            df_target = df_target[~df_target["symbole"].isin(symbols_stopped)]

        if not df_target.empty:
            logger.info("🎯 Allocation Cible Calculée (Momentum + Risk Parity) :")
            for _, t in df_target.iterrows():
                logger.info(
                    f"   - {t['symbole']} ({t['secteur']}) : Poids Cible = {t['target_weight']*100:.2f}% | Momentum = {t['momentum']*100:+.2f}% | Vol = {t['volatilite']*100:.2f}%"
                )

        # 6. Génération et exécution des ordres de rééquilibrage
        # Priorité aux ventes pour libérer du cash avant d'acheter
        rebal_orders = self.compute_rebalancing_orders(df_target, df_positions, valeur_totale, solde_cash)

        ventes = [o for o in rebal_orders if o["sens"] == "VENTE" and o["symbole"] not in symbols_stopped]
        achats = [o for o in rebal_orders if o["sens"] == "ACHAT"]

        logger.info(f"Planification : {len(ventes)} ordre(s) de vente, {len(achats)} ordre(s) d'achat.")

        # Exécuter les ventes
        for v in ventes:
            self.execute_order(v["sens"], v["symbole"], v["quantite"], v["raison"])

        # Exécuter les achats
        for a in achats:
            self.execute_order(a["sens"], a["symbole"], a["quantite"], a["raison"])

        logger.info("Fin du cycle de trading.")
        logger.info("=" * 60)

    # =================================================================
    # Planification & Boucle Infinie 24/7
    # =================================================================
    def get_seconds_until_next_cycle(self) -> int:
        """
        Détermine le temps d'attente optimal jusqu'au prochain point de marché.
        Tente d'interroger prochaine_mise_a_jour() ou prochain_releve() via RPC.
        """
        # 1. Tentative d'interrogation du backend Supabase (prochaine_mise_a_jour ou prochain_releve)
        for rpc_name in ["prochaine_mise_a_jour", "prochain_releve"]:
            try:
                res = self.supabase.rpc(rpc_name).execute()
                if res.data:
                    target_dt = pd.to_datetime(res.data)
                    # Si c'est prochain_releve (qui est l'heure de cotation théorique), on ajoute le buffer
                    if rpc_name == "prochain_releve":
                        target_dt += timedelta(minutes=DELAY_BUFFER_MINUTES)
                    now = datetime.now(target_dt.tzinfo if target_dt.tzinfo else None)
                    diff = (target_dt - now).total_seconds()
                    if diff > 5:
                        logger.info(f"Prochain relevé serveur via RPC '{rpc_name}' : {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (dans {int(diff)}s / {int(diff)//60} min)")
                        return int(diff)
            except Exception as e:
                logger.debug(f"RPC {rpc_name} non disponible : {e}")

        # 2. Calcul local basé sur les 10 relevés fixes par jour
        now = datetime.now()
        today = now.date()

        candidates = []
        for hour, minute in FIXED_MARKET_HOURS:
            # Heure officielle + délai de parution
            dt_cand = datetime(today.year, today.month, today.day, hour, minute) + timedelta(minutes=DELAY_BUFFER_MINUTES)
            if dt_cand > now:
                candidates.append(dt_cand)

        # Si tous les relevés du jour sont passés, cibler le premier relevé de demain
        if not candidates:
            tomorrow = today + timedelta(days=1)
            first_h, first_m = FIXED_MARKET_HOURS[0]
            dt_cand = datetime(tomorrow.year, tomorrow.month, tomorrow.day, first_h, first_m) + timedelta(minutes=DELAY_BUFFER_MINUTES)
            candidates.append(dt_cand)

        next_time = min(candidates)
        wait_seconds = int((next_time - now).total_seconds())
        logger.info(f"Prochain cycle programmé à : {next_time.strftime('%Y-%m-%d %H:%M:%S')} (dans {wait_seconds}s / {wait_seconds//60} min)")
        return max(wait_seconds, 30)

    def start_bot_loop(self):
        """Lance la boucle permanente 24h/24."""
        logger.info("🚀 Démarrage du Bot Parquet CAC 40 en boucle continue 24/7...")
        # Exécuter un premier cycle immédiatement au lancement
        try:
            self.run_strategy_cycle()
        except Exception as e:
            logger.error(f"Erreur lors du cycle initial : {e}", exc_info=True)

        while True:
            try:
                sleep_seconds = self.get_seconds_until_next_cycle()
                logger.info(f"Mise en veille du bot pour {sleep_seconds} secondes...")
                time.sleep(sleep_seconds)

                # Réveil et exécution du cycle
                self.run_strategy_cycle()
            except KeyboardInterrupt:
                logger.info("Arrêt manuel demandé par l'utilisateur. Sortie propre du bot.")
                break
            except Exception as e:
                logger.error(f"Erreur inattendue dans la boucle principale : {e}", exc_info=True)
                logger.info("Nouvelle tentative dans 60 secondes...")
                time.sleep(60)


# =====================================================================
# Point d'entrée principal
# =====================================================================
if __name__ == "__main__":
    bot = ParquetCAC40Bot()
    bot.start_bot_loop()
