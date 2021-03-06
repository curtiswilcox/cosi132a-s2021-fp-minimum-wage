import spacy
import re
import ssl
from nltk.corpus import wordnet
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from collections import defaultdict
from es_service.doc_template import BaseDoc
from typing import List
from elasticsearch_dsl import Search
from elasticsearch_dsl.connections import connections
from elasticsearch_dsl.query import Ids, Match
from embedding_service.client import EmbeddingClient
from embedding_service.text_processing import TextProcessing
from example_query import generate_script_score_query
from flask import Flask, render_template, request, jsonify

try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context

# magic
spacy.prefer_gpu
nlp = spacy.load("en_core_web_sm")

# list of words that are important to topic for use in query expansion / summarization
# naïve way of doing so, but oh well
keywords = ['federal', 'minimum', 'wage', 'increase', 'president',
            'congress', 'increase', 'united states', 'us', 'action', 'advocacy',
            'government', 'worker', 'contract', 'authority', 'bureau', 'labor', 'governor',
            'acts', 'law', 'bill', 'workforce', 'supreme court', 'states', 'state']

# mapping from state/territory name to abbreviation for use in query expansion / summarization
states = ['Alabama', 'AL',
          'Alaska', 'AK',
          'American Samoa', 'AS',
          'Arizona', 'AZ',
          'Arkansas', 'AR',
          'California', 'CA',
          'Colorado', 'CO',
          'Connecticut', 'CT',
          'Delaware', 'DE',
          'District of Columbia', 'DC',
          'Florida', 'FL',
          'Georgia', 'GA',
          'Guam', 'GU',
          'Hawaii', 'HI',
          'Idaho', 'ID',
          'Illinois', 'IL',
          'Indiana', 'IN',
          'Iowa', 'IA',
          'Kansas', 'KS',
          'Kentucky', 'KY',
          'Louisiana', 'LA',
          'Maine', 'ME',
          'Maryland', 'MD',
          'Massachusetts', 'MA',
          'Michigan', 'MI',
          'Minnesota', 'MN',
          'Mississippi', 'MS',
          'Missouri', 'MO',
          'Montana', 'MT',
          'Nebraska', 'NE',
          'Nevada', 'NV',
          'New Hampshire', 'NH',
          'New Jersey', 'NJ',
          'New Mexico', 'NM',
          'New York', 'NY',
          'North Carolina', 'NC',
          'North Dakota', 'ND',
          'Northern Mariana Islands', 'MP',
          'Ohio', 'OH',
          'Oklahoma', 'OK',
          'Oregon', 'OR',
          'Pennsylvania', 'PA',
          'Puerto Rico', 'PR',
          'Rhode Island', 'RI',
          'South Carolina', 'SC',
          'South Dakota', 'SD',
          'Tennessee', 'TN',
          'Texas', 'TX',
          'Utah', 'UT',
          'Vermont', 'VT',
          'Virgin Islands', 'VI',
          'Virginia', 'VA',
          'Washington', 'WA',
          'West Virginia', 'WV',
          'Wisconsin', 'WI',
          'Wyoming', 'WY']

states_lower = [state.lower() for state in states]
important = keywords + states_lower
stop_words = set(stopwords.words('english'))

app = Flask(__name__)

RESULTS_PER_PAGE = 8

query = None
method = None
all_docs = []
documents = []
num_results = 0
results_back = 0

# text processor for query using customized processor
text_processor = TextProcessing.from_nltk()


# home page
@app.route("/")
def home():
    return render_template("home.html")


# result page
@app.route("/results", methods=["POST"])
def results():
    """
    result page
    :return: page that shows the first eight results (or all results if there were <= eight)
    """
    global query
    global method
    global all_docs
    global documents
    global num_results
    global results_back

    query_input = request.form['query']
    # query processing
    query_list: List[str] = query_input.split(" ")
    query_n = normalize_query(query_list)
    query = general_query_processing(query_n)
    # query = query_input
    method = request.form['method']
    ranker = method.split('-')[0]
    analyzer = method.split('-')[1]
    results_back = int(request.form['num-results'])
    all_docs, documents = get_documents(query, analyzer, ranker, results_back)

    # calculate the total number of results found
    num_results = sum(len(ds) for ds in documents.values())

    return render_template("results.html", query=query_input, docs=documents[1], num_results=num_results,
                           page_id=1, method=method, res_per_page=RESULTS_PER_PAGE,
                           results_back=results_back,
                           last_page=(num_results == len(documents[1])))  # add variables as you wish


# "next page" to show more results
@app.route("/results/<int:page_id>", methods=["POST"])
def next_page(page_id):
    """
    "next page" to show more results
    :param page_id: the page to display
    :return: next page of results
    """
    global query  # ugh
    global documents  # blech
    global num_results  # gross
    global method
    global results_back
    # print(documents[page_id])
    return render_template("results.html", query=query, docs=documents[page_id],
                           num_results=num_results, page_id=page_id, method=method,
                           res_per_page=RESULTS_PER_PAGE, results_back=results_back,
                           last_page=(num_results <= page_id * RESULTS_PER_PAGE))  # add variables as you wish


# document page
@app.route("/doc_data/<doc_id>")
def doc_data(doc_id):
    """
    document page
    :param doc_id: document to display
    :return: page containing information on the selected document
    """
    global all_docs
    return render_template("doc.html", info=all_docs[doc_id])


# search "page" (autocompletion)
@app.route('/search', methods=['POST'])
def search():
    """
    search "page" to give the auto-completion when typing in suggestions
    :return: jsonified list of possible autocompletion matches
    """
    search_term = request.form['q']
    connections.create_connection(hosts=['localhost'], timeout=100, alias='default')

    # create a search object over the document type being used
    s = BaseDoc.search()
    s = s.suggest('title_suggestions', search_term, completion={'field': 'title_suggest'})
    response = s.execute()
    # print(response.suggest.title_suggestions)
    suggestions = [option.text
                   for result in response.suggest.title_suggestions
                   for option in result.options]

    resp = jsonify(suggestions)
    resp.status_code = 200
    return resp


def get_documents(query, analyzer, ranker, results_back):
    """
    get the documents that match the query, using the specified analyzer and reranker (if applicable)
    :param query: query to match on
    :param analyzer: analyzer to use
    :param ranker: reranker to use
    :param results_back: how many results to return
    :return: list of documents that match parameters
    """
    connections.create_connection(hosts=["localhost"], timeout=100, alias="default")
    search = bm25_documents(query, analyzer, results_back)  # out here because need it for both
    if ranker == 'bm25':  # do no more work, just process the fp_data
        return form_result_list(search.execute())
    else:  # rerank
        new_search = embedding_documents(query, search, ranker, results_back)
        return form_result_list(new_search.execute())


def bm25_documents(query, analyzer, results_back):
    """
    use the BM25 algorithm to find documents that match the query
    :param query: query to match on
    :param analyzer: analyzer to use
    :param results_back: how many results to return
    :return: `Search` object to match documents
    """
    if analyzer == 'default':
        q = Match(content={'query': query})
    elif analyzer == 'n_gram':
        q = Match(n_gram_custom_content={'query': query})
    elif analyzer == 'whitespace':
        q = Match(whitespace_custom_content={'query': query})
    return Search(using='default', index='wapo_docs_50k').query(q)[:results_back]


def embedding_documents(query, bm_search, ranker, results_back):
    """
    use the selected reranker to rerank documents found by BM25
    :param query: query to match on
    :param bm_search: results of the BM25 algorithm
    :param ranker: reranker to use
    :param results_back: number of results to return
    :return: `Search` object to match on reranked documents
    """
    ids = [hit.meta.id for hit in bm_search.execute()]
    q_match_ids = Ids(values=ids)

    encoder = EmbeddingClient(host='localhost', embedding_type=ranker)
    # noinspection PyTypeChecker
    embedding = encoder.encode([query], pooling="mean").tolist()[0]
    vector_name = ('ft' if ranker == 'fasttext' else 'sbert') + '_vector'
    q_vector = generate_script_score_query(embedding, vector_name)

    compound = (q_match_ids & q_vector)

    return Search(using='default', index='wapo_docs_50k').query(compound)[:results_back]


def form_result_list(docs):
    """
    put all documents matched in a dictionary to easily render documents per page for the user
    :param docs: documents that were matched / to display
    :return: tuple: dictionary of doc id to relevant document, defaultdict of docs by page they appear on,
                    and sorted by relevance (most relevant appear first)
    """
    paged_docs = defaultdict(list)
    i = 1
    # print([(hit.title, hit.annotation) for hit in sorted(docs, key=get_hit_key, reverse=True)])
    # docs = sorted(docs, key=get_hit_key, reverse=True)  # maybe lose this
    for doc in docs:
        if len(paged_docs[i]) == RESULTS_PER_PAGE:
            i += 1
        paged_docs[i].append(
            {'doc_id': doc.doc_id, 'title': doc.title,
             'author': doc.author, 'date': doc.date, 'content': doc.content,
             'annotation': doc.annotation}
        )

    return {el['doc_id']: el for lst in paged_docs.values() for el in lst}, paged_docs


def normalize_query(query_list):
    """
    return a normalized query with the customized text processor
    :param query_list: query string in a list, separated by comma
    :return: normalized query
    """
    normalized_q = []
    for q in query_list:
        normalized_q.append(text_processor.normalize(q, True))
    return " ".join(normalized_q)


def general_query_processing(query):
    """
    query optimization: if too few words --> query expansion; if too many --> text summary; if in-between --> original
    :param query: user input
    :return: processed query
    """
    query_no_punc = re.sub(r'[^\w\s]', '', query)
    query_lower = query_no_punc.lower()
    query_list_lower = word_tokenize(query_lower)
    filtered_sentence_l = [w for w in query_list_lower if w not in stop_words]
    filtered_sentence_str_l = " ".join(filtered_sentence_l)
    # keep the upper case as it is for spacy
    filtered_sentence_og = [w for w in query_no_punc if w not in stop_words]
    filtered_sentence_str_og = " ".join(filtered_sentence_og)

    if len(filtered_sentence_l) <= 3:
        expanded_q = query_expansion(filtered_sentence_l)
        return expanded_q
    elif len(filtered_sentence_l) >= 8:
        q_summary = query_summary(filtered_sentence_l, filtered_sentence_str_og)
        # UNCOMMENT THE LINE BELOW IF YOU WANT TO TEST TERM FREQUENCY
        # q_summary = query_summary_freq(filtered_sentence_l)
        return q_summary
    else:
        return filtered_sentence_str_l


def query_expansion(query):
    """
    expand the query using wordnet from NLTK
    :param query: query to expand
    :return: expanded query
    """
    synonyms = set()
    for q in query:
        syn = wordnet.synsets(q)
        for s in syn:
            for lm in s.lemmas():
                if str(lm.name()).find("_") == -1:
                    synonyms.add(str(lm.name().lower()))  # adding into synonyms
    list_of_strings = [str(s) for s in synonyms]
    return " ".join(list_of_strings)


def query_summary(query, query_str):
    """
    only extract important words from the query, such as noun and verb or words in keywords or states lists
    :param query: query to summarize (as list from the original query, split on spaces)
    :param query_str: query to summarize in string form
    :return: summarized query
    """
    l = []
    # get named entity recognition using pretrained pipline from spaCy
    doc = nlp(query_str)
    for ent in doc.ents:
        if ent.label_ in ['PERSON', 'GPE', 'NORP', 'ORG', 'TIME', 'CARDINAL', 'MONEY', 'EVENT']:
            l.append(str(ent.text).lower())
    # add to the set if the query token is in the important word list, manually defined
    for q in query:
        if q in important and q not in l:
            l.append(q)

    return " ".join(l)


def query_summary_freq(query, scalar=1.2):
    """
    summarize long query by their frequencies
    :param query: query in list form to get more frequent terms from
    :param scalar: used to determine the threshold relative to the average frequency (scalar is a float)
    :return: more frequent words in query (string, elements obtained from words in query that have a frequency above the set threshold)
    """
    freq = {}
    for q in query:
        if q in freq:
            freq[q] += 1
        else:
            freq[q] = 1

    net_freq = sum(freq.values())
    ele_num = len(freq.keys())
    threshold = float(net_freq / ele_num) * scalar

    high_freq = []
    for token in freq:
        if freq[token] >= threshold:
            high_freq.append(token)

    return " ".join(high_freq)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
