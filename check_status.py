#!/usr/bin/env python3
import requests
from parquet_bot import ParquetCAC40Bot

print("=== 1. VÉRIFICATION DU RUN GITHUB ACTIONS ===")
try:
    url = "https://api.github.com/repos/Aureliengrl/datacourse/actions/runs"
    headers = {"Accept": "application/vnd.github+json"}
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        data = r.json()
        runs = data.get("workflow_runs", [])
        if runs:
            latest = runs[0]
            print("Run ID:", latest.get("id"))
            print("Nom du workflow:", latest.get("name"))
            print("Statut:", latest.get("status"))
            print("Conclusion:", latest.get("conclusion"))
            print("Déclenché le:", latest.get("created_at"))
            print("URL du run:", latest.get("html_url"))
        else:
            print("Aucun run GitHub Actions trouvé.")
    else:
        print(f"Erreur API GitHub ({r.status_code}): {r.text}")
except Exception as e:
    print(f"Erreur GitHub: {e}")

print("\n=== 2. VÉRIFICATION DE VOTRE COMPTE SUR LE SITE (SUPABASE) ===")
bot = ParquetCAC40Bot()
print("ID Etudiant:", bot.etudiant_id)
solde, total = bot.fetch_portefeuille()
print(f"Solde Cash: {solde:,.2f} € | Valeur Totale: {total:,.2f} €")

# Vérification du rang
rang = bot.fetch_mon_rang()
print(f"Rang actuel: {rang}")

# Vérification des ordres en attente
try:
    res = bot.supabase.rpc("mes_ordres_en_attente").execute()
    ordres_attente = res.data or []
    print(f"\nOrdres en attente d'exécution au prochain relevé ({len(ordres_attente)}) :")
    for o in ordres_attente:
        print("  ->", o)
except Exception as e:
    print(f"Erreur ordres en attente: {e}")

# Vérification de la table ordres
try:
    res = bot.supabase.table("ordres").select("*").order("saisi_le", desc=True).limit(15).execute()
    ordres_historique = res.data or []
    print(f"\nHistorique de tous les ordres saisis ({len(ordres_historique)}) :")
    for o in ordres_historique:
        print("  ->", o)
except Exception as e:
    print(f"Erreur historique ordres: {e}")

# Vérification des positions
positions = bot.fetch_positions()
print(f"\nPositions ouvertes ({len(positions)}) :")
if not positions.empty:
    print(positions.to_string())
else:
    print("  Aucune position ouverte pour le moment.")
