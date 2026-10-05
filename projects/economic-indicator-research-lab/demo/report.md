# 経済統計レポート（SYNTHETIC DEMO｜架空の国・架空の数値）

対象：AAA, BBB / 2020–2024年

このデモから実際の国の景気や投資判断について結論は出せません。

## 観察

- AAAの2024年CPI（2020=100）は115.00。基準年からの変化は15.00%。2020→2024年の年平均変化率は3.56%。
- BBBの2024年CPI（2020=100）は110.00。基準年からの変化は10.00%。2020→2024年の年平均変化率は2.41%。

## 記述統計（観測値のみ・補完なし）

| 国 | 指標 | 単位 | 観測数 | 平均 | 中央値 | 標準偏差 | 最小（年） | 最大（年） |
|---|---|---|---|---|---|---|---|---|
| AAA | 消費者物価指数 | index (2020=100) | 5/5 | 106.80 | 106.00 | 6.22 | 100.00 (2020) | 115.00 (2024) |
| AAA | インフレ率 | annual % | 5/5 | 3.04 | 3.60 | 1.50 | 1.00 (2020) | 4.70 (2023) |
| AAA | 実質GDP成長率 | annual % | 5/5 | 1.10 | 1.50 | 1.88 | -2.00 (2020) | 3.00 (2021) |
| BBB | 消費者物価指数 | index (2020=100) | 4/5 | 103.75 | 102.50 | 4.50 | 100.00 (2020) | 110.00 (2024) |
| BBB | インフレ率 | annual % | 4/5 | 1.50 | 1.50 | 1.29 | 0.00 (2020) | 3.00 (2022) |
| BBB | 実質GDP成長率 | annual % | 5/5 | 1.30 | 1.50 | 1.92 | -1.00 (2020) | 4.00 (2021) |

率の平均は各年の単純平均です。標準偏差は標本標準偏差です。

## 品質と限界

30観測枠中2件が欠損。補完せず対象年をそろえます。

- AAA：公表インフレ率とCPIから計算した前年比の最大差は0.02ポイント（両方が観測された年のみ）。
- BBB：公表インフレ率とCPIから計算した前年比の最大差は0.03ポイント（両方が観測された年のみ）。

指数の水準は国間の物価の高さを示しません。比較から因果関係・将来予測は導きません。

## 方法・定義の出典

- [World Bank Indicator API Queries](https://datahelpdesk.worldbank.org/knowledgebase/articles/898599-indicator-api-queries)
- [World Bank API Basic Call Structures](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581-api-basic-call-structures)
- [World Bank CPI metadata](https://databank.worldbank.org/metadataglossary/world-development-indicators/series/FP.CPI.TOTL)
