from sqlalchemy import select
from sqlalchemy.orm import Session
from database import Base, SessionLocal, engine
from models import ProviderRule, ProviderRate, UserSettings
from presyo_client import extract_provider_code, slugify


def normalize_existing_provider_code(provider_name: str) -> str:
    if not provider_name:
        return "UNKNOWN"
    # PRESYO-PH formats names as "ACRONYM - Full Utility Name"
    if " - " in provider_name:
        prefix = provider_name.split(" - ", 1)[0].strip()
        if prefix:
            return slugify(prefix)
    return extract_provider_code(provider_name)


def cleanup_database_duplicates():
    """
    One-time script to merge duplicate provider codes, remap saved user settings,
    and remove stale providers with no rates.
    """
    if engine is None:
        print("Database engine is not initialized. Cannot run cleanup.")
        return

    Base.metadata.create_all(bind=engine)

    db: Session = SessionLocal()
    try:
        print(f"Starting database cleanup on: {engine.url.database}...")
        providers = db.query(ProviderRule).all()

        if not providers:
            print("No provider records found in this database. Nothing to clean up.")
            return

        migrated_count = 0
        deleted_count = 0

        for old_provider in providers:
            correct_code = normalize_existing_provider_code(old_provider.provider_name)

            if old_provider.provider_code != correct_code:
                print(f"Migrating code: {old_provider.provider_code} -> {correct_code}")

                target_provider = (
                    db.query(ProviderRule)
                    .filter(ProviderRule.provider_code == correct_code)
                    .first()
                )
                if not target_provider:
                    target_provider = ProviderRule(
                        provider_code=correct_code,
                        provider_name=old_provider.provider_name,
                        region=old_provider.region,
                        base_distribution_fee=old_provider.base_distribution_fee,
                        tier_1_max_kwh=old_provider.tier_1_max_kwh,
                        tier_1_rate=old_provider.tier_1_rate,
                        tier_2_max_kwh=old_provider.tier_2_max_kwh,
                        tier_2_rate=old_provider.tier_2_rate,
                        tier_3_max_kwh=old_provider.tier_3_max_kwh,
                        tier_3_rate=old_provider.tier_3_rate,
                        tier_4_rate=old_provider.tier_4_rate,
                    )
                    db.add(target_provider)
                    db.flush()

                old_rates = (
                    db.query(ProviderRate)
                    .filter(ProviderRate.provider_code == old_provider.provider_code)
                    .all()
                )
                for rate in old_rates:
                    existing_target_rate = (
                        db.query(ProviderRate)
                        .filter(
                            ProviderRate.provider_code == correct_code,
                            ProviderRate.billing_month == rate.billing_month,
                        )
                        .first()
                    )
                    if existing_target_rate:
                        db.delete(rate)
                    else:
                        rate.provider_code = correct_code

                db.query(UserSettings).filter(
                    UserSettings.selected_provider == old_provider.provider_code
                ).update({"selected_provider": correct_code})

                db.delete(old_provider)
                migrated_count += 1

        db.flush()

        active_codes = select(ProviderRate.provider_code).distinct()
        stale_providers = (
            db.query(ProviderRule)
            .filter(ProviderRule.provider_code.not_in(active_codes))
            .all()
        )
        for stale in stale_providers:
            print(f"Deleting stale provider with no rates: {stale.provider_code}")
            db.delete(stale)
            deleted_count += 1

        db.commit()
        print(f"Cleanup complete. Migrated {migrated_count} duplicates and deleted {deleted_count} stale records.")

    except Exception as e:
        db.rollback()
        print(f"Cleanup failed: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    cleanup_database_duplicates()