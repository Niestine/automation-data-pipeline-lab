# 一次資料を実装に適用した箇所

- **World Bank, Indicator API Queries**  
  https://datahelpdesk.worldbank.org/knowledgebase/articles/898599-indicator-api-queries  
  複数指標をまとめて取得するときは出典ID（`source`）を指定する、という規則に従い `source=2`（WDI）で3指標を1リクエストにまとめます。`SERIES` で指標コード・単位を固定し、未対応の指標コードや年次以外の日付（例：`2020Q1`）は `parse_pages` で拒否します（`test_api_error_unknown_series_nonannual`）。`provenance.json` では方法資料（`methodology_sources`）と数値の取得URL（`request_urls`）を分けて記録します（`test_report_origin_and_hash`）。
- **World Bank, API Basic Call Structures**  
  https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures  
  `format=json`、`date=開始:終了`、`page`、`per_page` による取得と、レスポンス先頭のページ情報（`page` / `pages` / `total`）を使った完全性検査を `fetch_live`・`page_meta`・`parse_pages` に実装しています。ページ不足・件数不一致・重複ページ・要求と異なるページ・エラー応答・メタデータ欠落を、ネットワークを使わないAPI形状のフィクスチャで検証します（`test_partial_and_inconsistent_pages`、`test_mock_live_pagination_no_network`、`test_live_page_mismatch_and_bad_scope`、`test_malformed_items_and_metadata_raise_valueerror`）。
- **World Bank, Consumer price index (2010=100) metadata**  
  https://databank.worldbank.org/metadataglossary/world-development-indicators/series/FP.CPI.TOTL  
  CPIは基準年を100とする指数であり、指数水準と年次の変化率は別物、という定義を反映しています。`rebase` で正の観測値がある共通基準年に換算し、`yoy` は直前年が観測されている場合だけ変化率を計算します。基準値の欠損・ゼロ・負値、欠損年をまたぐ計算をしないことをテストします（`test_rebase_and_percentage_points`、`test_invalid_cpi_base`、`test_yoy_does_not_bridge_years`）。

## 文献の手法ではない独自設計

次は一般的な統計処理・品質管理として独自に設計したもので、上の資料の手法とは呼びません。

- 観測値のみの記述統計（`describe`）と、期間の両端が観測された場合だけのCPI年平均変化率（`annualized_change`）。`test_descriptive_statistics_skip_missing`、`test_summary_and_inflation_consistency_on_demo`。
- 公表インフレ率とCPIから計算した前年比の差（パーセントポイント）の確認。置き換えや補正はしません。
- null / 未掲載 / ゼロの区別、重複・非有限値の拒否、スナップショットのSHA-256と出力の再現性テスト（`test_snapshot_hash_matches_file_bytes`、`test_committed_demo_is_reproducible_and_snapshot_rerenders`）。

原本と抽出文は制作側に保存し、公開プロジェクトにはURLと短い適用説明のみを含めます。架空の数値から実際の経済状況や投資判断を導きません。
