import contextlib,copy,hashlib,importlib.util,io,json,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location("econ",ROOT/"pipeline.py");p=importlib.util.module_from_spec(s);s.loader.exec_module(p)
class EconomicsTests(unittest.TestCase):
 def setUp(self):self.data=json.loads((ROOT/"fixtures/demo.json").read_text(encoding="utf-8"))
 def test_zero_null_and_coverage(self):
  r=p.analyze(p.parse_pages(self.data["pages"]),["AAA","BBB"],2020,2024,2020)
  self.assertEqual((r["expected_cells"],r["observed_cells"]),(30,28))
  self.assertTrue(any(x["value"]==0 and x["status"]=="observed" for x in r["grid"]))
 def test_rebase_and_percentage_points(self):
  self.assertEqual(p.rebase({2020:50,2021:60,2022:None},2020),{2020:100,2021:120,2022:None})
  self.assertEqual(p.pp(3,2),1);self.assertIsNone(p.pp(None,2))
 def test_invalid_cpi_base(self):
  for v in [{2020:None},{2020:0},{2020:1,2021:-1}]:
   with self.assertRaises(ValueError):p.rebase(v,2020)
 def test_yoy_does_not_bridge_years(self):
  self.assertTrue(all(v is None for v in p.yoy({2020:100,2022:121,2023:None,2024:140}).values()))
  self.assertAlmostEqual(p.yoy({2020:100,2021:110})[2021],10)
 def test_partial_and_inconsistent_pages(self):
  with self.assertRaises(ValueError):p.parse_pages(self.data["pages"][:1])
  for mode in ["count","duplicate"]:
   pages=copy.deepcopy(self.data["pages"])
   if mode=="count":pages[1][0]["total"]=99
   else:pages[1][0]["page"]=1
   with self.assertRaises(ValueError):p.parse_pages(pages)
 def test_duplicate_record(self):
  pages=copy.deepcopy(self.data["pages"]);pages[1][1][0]=copy.deepcopy(pages[0][1][0])
  with self.assertRaises(ValueError):p.parse_pages(pages)
 def test_nonfinite_bool_text(self):
  for v in [float("nan"),float("inf"),True,"1.5"]:
   pages=copy.deepcopy(self.data["pages"]);pages[0][1][0]["value"]=v
   with self.assertRaises(ValueError):p.parse_pages(pages)
 def test_api_error_unknown_series_nonannual(self):
  with self.assertRaises(ValueError):p.parse_pages([[{"message":[]} ]])
  for k,v in [("date","2020Q1"),("indicator",{"id":"OTHER"})]:
   pages=copy.deepcopy(self.data["pages"]);pages[0][1][0][k]=v
   with self.assertRaises(ValueError):p.parse_pages(pages)
 def test_unexpected_country_and_absent_cell(self):
  rows=p.parse_pages(self.data["pages"])
  with self.assertRaises(ValueError):p.analyze(rows,["AAA"],2020,2024,2020)
  rows=[r for r in rows if (r["country"],r["indicator"],r["year"])!=("BBB","FP.CPI.TOTL.ZG",2024)]
  r=p.analyze(rows,["AAA","BBB"],2020,2024,2020)
  self.assertTrue(any(x["reason"]=="absent" and x["year"]==2024 for x in r["missing"]))
 def test_mock_live_pagination_no_network(self):
  calls=[]
  def get(url):calls.append(url);return copy.deepcopy(self.data["pages"][len(calls)-1])
  result=p.fetch_live(["AAA","BBB"],2020,2024,get)
  self.assertEqual(result["data_origin"],"world_bank_live");self.assertEqual(len(calls),2);self.assertIn("source=2",calls[0])
  with tempfile.TemporaryDirectory() as d:
   p.write_report(result,Path(d),2020)
   prov=json.loads((Path(d)/"provenance.json").read_text(encoding="utf-8"))
   self.assertEqual(prov["request_urls"],calls);self.assertTrue(all(u.startswith("https://api.worldbank.org/v2/country/AAA;BBB/indicator/") for u in calls))
   self.assertTrue(all("worldbank.org" in u and "api.worldbank" not in u for _,u in prov["methodology_sources"]))
   self.assertIn("WORLD BANK",(Path(d)/"report.md").read_text(encoding="utf-8"))
 def test_url_injection_and_page_limit(self):
  with self.assertRaises(ValueError):p.fetch_live(["JPN?redirect=x"],2020,2024,lambda _:None)
  with self.assertRaises(ValueError):p.fetch_live(["JPN"],2020,2024,lambda _:[{"pages":21},[]])
 def test_report_origin_and_hash(self):
  with tempfile.TemporaryDirectory() as d:
   p.write_report(self.data,Path(d),2020)
   h=(Path(d)/"report.html").read_text(encoding="utf-8")
   self.assertIn("SYNTHETIC DEMO",h);self.assertIn("架空の国・架空の数値",h)
   prov=json.loads((Path(d)/"provenance.json").read_text(encoding="utf-8"))
   self.assertEqual(prov["data_origin"],"synthetic_fixture");self.assertEqual(len(prov["snapshot_sha256"]),64);self.assertEqual(len(prov["methodology_sources"]),3)
   self.assertIn("synthetic_fixture",(Path(d)/"indicators.csv").read_text(encoding="utf-8-sig"))
   self.assertIn("記述統計",h);self.assertIn("n_observed",(Path(d)/"summary.csv").read_text(encoding="utf-8-sig"))
 def test_snapshot_hash_matches_file_bytes(self):
  with tempfile.TemporaryDirectory() as d:
   p.write_report(self.data,Path(d),2020)
   prov=json.loads((Path(d)/"provenance.json").read_text(encoding="utf-8"))
   self.assertEqual(prov["snapshot_sha256"],hashlib.sha256((Path(d)/prov["snapshot_file"]).read_bytes()).hexdigest())
 def test_committed_demo_is_reproducible_and_snapshot_rerenders(self):
  with tempfile.TemporaryDirectory() as d:
   p.write_report(self.data,Path(d)/"a",2020)
   snapshot=json.loads((Path(d)/"a"/"raw_snapshot.json").read_text(encoding="utf-8"))
   p.write_report(snapshot,Path(d)/"b",2020)
   names=sorted(f.name for f in (ROOT/"demo").iterdir())
   self.assertEqual(names,sorted(f.name for f in (Path(d)/"a").iterdir()))
   for name in names:
    self.assertEqual((ROOT/"demo"/name).read_bytes(),(Path(d)/"a"/name).read_bytes(),name)
    self.assertEqual((Path(d)/"a"/name).read_bytes(),(Path(d)/"b"/name).read_bytes(),name)
 def test_descriptive_statistics_skip_missing(self):
  s=p.describe({2020:1.0,2021:None,2022:3.0,2023:2.0})
  self.assertEqual((s["n_observed"],s["n_expected"],s["mean"],s["median"],s["min_year"],s["max_year"]),(3,4,2.0,2.0,2020,2022))
  self.assertAlmostEqual(s["stdev"],1.0)
  self.assertIsNone(p.describe({2020:5.0})["stdev"]);self.assertIsNone(p.describe({2020:None})["mean"])
  self.assertAlmostEqual(p.annualized_change({2020:100,2022:121},2020,2022),10)
  self.assertIsNone(p.annualized_change({2020:100,2022:None},2020,2022))
 def test_summary_and_inflation_consistency_on_demo(self):
  r=p.analyze(p.parse_pages(self.data["pages"]),["AAA","BBB"],2020,2024,2020)
  row={(x["country"],x["indicator"]):x for x in r["summary"]}
  self.assertEqual(row[("BBB","FP.CPI.TOTL.ZG")]["n_observed"],4)
  self.assertAlmostEqual(row[("AAA","FP.CPI.TOTL.ZG")]["mean"],3.04)
  self.assertAlmostEqual(r["derived"]["AAA"]["cpi_annualized_change_percent"],(1.15**0.25-1)*100)
  gaps=r["derived"]["BBB"]["published_minus_cpi_yoy_pp"]
  self.assertAlmostEqual(gaps[2022],3-(104/101-1)*100);self.assertIsNone(gaps[2023]);self.assertIsNone(gaps[2024])
 def test_malformed_items_and_metadata_raise_valueerror(self):
  for mutate in [lambda pg:pg[0][1].__setitem__(0,"x"),lambda pg:pg[0][1][0].__setitem__("indicator",None),lambda pg:pg[0][0].pop("total"),lambda pg:pg[0].__setitem__(1,None)]:
   pages=copy.deepcopy(self.data["pages"]);mutate(pages)
   with self.assertRaises(ValueError):p.parse_pages(pages)
 def test_live_page_mismatch_and_bad_scope(self):
  with self.assertRaises(ValueError):p.fetch_live(["AAA","BBB"],2020,2024,lambda _:copy.deepcopy(self.data["pages"][1]))
  rows=p.parse_pages(self.data["pages"])
  for args in [(["AAA","BBB"],2020,2024,2019),(["aaa","BBB"],2020,2024,2020),(["AAA","AAA"],2020,2024,2020)]:
   with self.assertRaises(ValueError):p.analyze(rows,*args)
 def test_cli_protects_synthetic_demo_folder(self):
  calls=[]
  orig=p.fetch_live;p.fetch_live=lambda *a,**k:calls.append(a)
  try:
   with contextlib.redirect_stderr(io.StringIO()) as err:self.assertEqual(p.main(["--live"]),1)
  finally:p.fetch_live=orig
  self.assertEqual(calls,[]);self.assertIn("--out",err.getvalue())
  with tempfile.TemporaryDirectory() as d:
   live=dict(copy.deepcopy(self.data),data_origin="world_bank_live");f=Path(d)/"snap.json";f.write_text(json.dumps(live),encoding="utf-8")
   with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(p.main(["--fixture",str(f)]),1)
   with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(p.main(["--fixture",str(f),"--out",str(Path(d)/"o")]),0)
   self.assertIn("WORLD BANK",(Path(d)/"o"/"report.html").read_text(encoding="utf-8"))
if __name__=="__main__":unittest.main()
