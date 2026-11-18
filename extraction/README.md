1. Run `fetch-subforums.js` in dev tools, dump in a CSV named subforums.csv
2. Run `cat subforums.csv | xargs -I {} sh -c "./crawl_subforum.py --since 2020-01-01 ---max-threads 600"`
3. Run `ls thread_*.csv | xargs -I {} sh -c "./crawl_questions.py {}"`

You will end up with a bunch of thread_{id}.csv files and their corresponding questions_{id}.csv


Execute:
```
mkdir dataset
mv subforums.csv dataset/
mv thread_*.csv dataset/
mv question_*.csv dataset/

zip -r dataset.zip dataset/
```

Upload `legal-dataset-intro.ipynb` to Colab, connect a runtime and upload dataset.zip to the Colab notebook's Files section



