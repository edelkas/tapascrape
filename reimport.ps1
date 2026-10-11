DUMPS="forumer_wayback_machine forumer_new_dump forumer_all_snapshots"
tapascrape metanet import-dump $DUMPS --db sqlite:///metanetfr.db
tapascrape metanet topic-flags $DUMPS --db sqlite:///metanetfr.db
tapascrape metanet link --db sqlite:///metanetfr.db
tapascrape metanet quotes --db sqlite:///metanetfr.db
tapascrape metanet attachments --db sqlite:///metanetfr.db
tapascrape metanet avatars --db sqlite:///metanetfr.db
tapascrape metanet site --db sqlite:///metanetfr.db --out site