tapascrape metanet import-dump forumer_wayback_machine forumer_new_dump forumer_all_snapshots --db sqlite:///metanetfr.db
tapascrape metanet topic-flags forumer_wayback_machine forumer_new_dump forumer_all_snapshots --db sqlite:///metanetfr.db
tapascrape metanet link --db sqlite:///metanetfr.db
tapascrape metanet quotes --db sqlite:///metanetfr.db
tapascrape metanet attachments --db sqlite:///metanetfr.db
tapascrape metanet avatars --db sqlite:///metanetfr.db
tapascrape metanet images --db sqlite:///metanetfr.db
tapascrape metanet site --db sqlite:///metanetfr.db --out site