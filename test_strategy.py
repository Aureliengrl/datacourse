#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests unitaires pour la logique factorielle et le moteur de gestion des risques.
"""

import unittest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from parquet_bot import ParquetCAC40Bot, MAX_STOCK_WEIGHT, MAX_SECTOR_WEIGHT, MIN_CASH_BUFFER, STOP_LOSS_THRESHOLD


class TestParquetBotStrategy(unittest.TestCase):

    def setUp(self):
        # Initialisation du bot en mode test sans auth Supabase
        self.bot = ParquetCAC40Bot.__new__(ParquetCAC40Bot)
        self.bot.supabase = None
        self.bot.session = None
        self.bot.etudiant_id = "test_etudiant"

    def test_apply_portfolio_constraints(self):
        """Vérifie que les contraintes par titre (15%) et par secteur (30%) sont respectées."""
        df_alloc = pd.DataFrame([
            {"symbole": "AIR", "secteur": "Industrie", "target_weight": 0.50},
            {"symbole": "SAF", "secteur": "Industrie", "target_weight": 0.30},
            {"symbole": "BNP", "secteur": "Finance", "target_weight": 0.15},
            {"symbole": "SAN", "secteur": "Santé", "target_weight": 0.05},
        ])

        df_res = self.bot._apply_portfolio_constraints(df_alloc)

        # 1. Vérification du plafond par titre (15%)
        for _, row in df_res.iterrows():
            self.assertLessEqual(row["target_weight"], MAX_STOCK_WEIGHT + 1e-5, f"Plafond titre dépassé pour {row['symbole']}")

        # 2. Vérification du plafond sectoriel (30%)
        for secteur, grp in df_res.groupby("secteur"):
            self.assertLessEqual(grp["target_weight"].sum(), MAX_SECTOR_WEIGHT + 1e-5, f"Plafond sectoriel dépassé pour {secteur}")

        # 3. Vérification du coussin de cash (Total actions <= 1 - MIN_CASH_BUFFER)
        self.assertLessEqual(df_res["target_weight"].sum(), (1.0 - MIN_CASH_BUFFER) + 1e-5)

    def test_check_stop_losses(self):
        """Vérifie le déclenchement strict du stop loss mécanique à -10%."""
        positions_data = [
            {"symbole": "TOTAL", "quantite": 100, "pru": 60.0, "dernier_cours": 65.0, "plus_value_pct": 8.33},
            {"symbole": "STELLANTIS", "quantite": 200, "pru": 20.0, "dernier_cours": 17.5, "plus_value_pct": -12.5},  # -12.5% -> STOP LOSS
            {"symbole": "LVMH", "quantite": 50, "pru": 700.0, "dernier_cours": 640.0, "plus_value_pct": -8.57},      # -8.57% -> OK
        ]
        df_positions = pd.DataFrame(positions_data)

        stop_orders = self.bot.check_stop_losses(df_positions)
        self.assertEqual(len(stop_orders), 1)
        self.assertEqual(stop_orders[0]["symbole"], "STELLANTIS")
        self.assertEqual(stop_orders[0]["sens"], "VENTE")
        self.assertEqual(stop_orders[0]["quantite"], 200)

    def test_compute_rebalancing_orders_threshold(self):
        """Vérifie que les petits écarts sous le seuil REBALANCE_THRESHOLD (3%) ne génèrent pas d'ordres."""
        df_target = pd.DataFrame([
            {"symbole": "AIR", "target_weight": 0.10, "dernier_cours": 100.0},
            {"symbole": "BNP", "target_weight": 0.12, "dernier_cours": 50.0},
        ])
        
        # Portefeuille total 100 000 €
        # AIR a déjà 9.5% (écart 0.5% < 3%) -> Pas d'ordre
        # BNP a 5.0% (écart 7% > 3%) -> Ordre d'achat
        df_positions = pd.DataFrame([
            {"symbole": "AIR", "quantite": 95, "dernier_cours": 100.0, "valeur": 9500.0},
            {"symbole": "BNP", "quantite": 100, "dernier_cours": 50.0, "valeur": 5000.0},
        ])

        orders = self.bot.compute_rebalancing_orders(df_target, df_positions, 100000.0, 50000.0)

        # Seul BNP doit être rééquilibré
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["symbole"], "BNP")
        self.assertEqual(orders[0]["sens"], "ACHAT")

    def test_cash_buffer_protection_5_percent(self):
        """Vérifie que les achats ne dépensent JAMAIS le coussin de 5% de cash (50 000 € sur 1M €)."""
        # Portefeuille 1 000 000 €, cash = 60 000 €
        # Seuil 5% = 50 000 € -> Seuls 10 000 € sont dépensables
        df_target = pd.DataFrame([
            {"symbole": "LVMH", "target_weight": 0.10, "dernier_cours": 500.0}, # voudrait acheter 100 000 €
        ])
        df_positions = pd.DataFrame() # Aucune position actuelle

        orders = self.bot.compute_rebalancing_orders(
            df_target=df_target,
            df_positions=df_positions,
            valeur_totale=1000000.0,
            solde_cash=60000.0
        )

        self.assertEqual(len(orders), 1)
        # Max possible = floor(10 000 / 500) = 20 actions = 10 000 €
        self.assertEqual(orders[0]["quantite"], 20)
        self.assertEqual(orders[0]["cout_estime"], 10000.0)


if __name__ == "__main__":
    unittest.main()
