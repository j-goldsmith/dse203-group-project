from hybrid_engine import HybridEngine
import pandas as pd

df = pd.DataFrame({
	'productid': [10001, 10002, 10003],
        'category': ['cat1', 'cat1', 'cat2'],
        'rank': [1, 2, 1],
        })
engine = HybridEngine()
engine.execute('drop table RecommendationColaborative')
engine.create('RecommendationColaborative')
engine.write('RecommendationColaborative', df)