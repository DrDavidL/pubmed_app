import streamlit as st
import aiohttp
import asyncio
from datetime import datetime
from typing import Dict, List, Tuple
from openai import OpenAI
from prompts import main_pubmed_prompt
import xml.etree.ElementTree as ET

# Function to create chat completion
@st.cache_data
def create_chat_completion(
    messages,
    model="gpt-4o",
    frequency_penalty=0,
    logit_bias=None,
    logprobs=False,
    top_logprobs=None,
    max_tokens=None,
    n=1,
    presence_penalty=0,
    response_format=None,
    seed=42,  # Adding seed for reproducibility
    stop=None,
    stream=False,
    include_usage=False,
    temperature=1,
    top_p=1,
    tools=None,
    tool_choice="none",
    user=None
):
    client = OpenAI()

    params = {
        "model": model,
        "messages": messages,
        "frequency_penalty": frequency_penalty,
        "logit_bias": logit_bias,
        "logprobs": logprobs,
        "top_logprobs": top_logprobs,
        "max_tokens": max_tokens,
        "n": n,
        "presence_penalty": presence_penalty,
        "response_format": response_format,
        "seed": seed,
        "stop": stop,
        "stream": stream,
        "temperature": temperature,
        "top_p": top_p,
        "user": user
    }

    if stream:
        params["stream_options"] = {"include_usage": include_usage}
    else:
        params.pop("stream_options", None)

    if tools:
        params["tools"] = [{"type": "function", "function": tool} for tool in tools]
        params["tool_choice"] = tool_choice

    if response_format == "json_object":
        params["response_format"] = {"type": "json_object"}
    elif response_format == "text":
        params["response_format"] = {"type": "text"}
    else:
        params.pop("response_format", None)

    params = {k: v for k, v in params.items() if v is not None}
    
    completion = client.chat.completions.create(**params)
    
    return completion

# Function to fetch article details with retry logic
async def fetch_article_details(session: aiohttp.ClientSession, id: str, details_url: str, abstracts_url: str, semaphore: asyncio.Semaphore) -> Tuple[str, Dict, str]:
    async with semaphore:
        for attempt in range(3):  # Retry up to 3 times for rate limit errors
            try:
                async with session.get(details_url) as details_response:
                    details_response.raise_for_status()
                    details_data = await details_response.json()
                
                async with session.get(abstracts_url) as abstracts_response:
                    abstracts_response.raise_for_status()
                    abstracts_data = await abstracts_response.text()
                
                if details_data and 'result' in details_data:
                    return id, details_data, abstracts_data
                else:
                    st.error(f"No details found for ID {id}, attempt {attempt + 1}")

            except aiohttp.ClientResponseError as e:
                if e.status == 429:  # Handle rate limit error
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    st.error(f"ClientResponseError for ID {id}: {e}")
                    raise
            except Exception as e:
                if attempt == 2:
                    st.error(f"Error fetching details for ID {id}: {e}")
                    return id, {}, ''
                else:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff on general errors

    return id, {}, ''


# Function to extract abstract from XML
async def extract_abstract_from_xml(xml_data: str, pmid: str) -> str:
    try:
        root = ET.fromstring(xml_data)
        for article in root.findall(".//PubmedArticle"):
            medline_citation = article.find("MedlineCitation")
            if medline_citation:
                pmid_element = medline_citation.find("PMID")
                if pmid_element is not None and pmid_element.text == pmid:
                    abstract_element = medline_citation.find(".//Abstract")
                    if abstract_element is not None:
                        abstract_texts = []
                        for elem in abstract_element.findall("AbstractText"):
                            label = elem.get("Label")
                            text = ET.tostring(elem, encoding='unicode', method='text').strip()
                            if label:
                                abstract_texts.append(f"{label}: {text}")
                            else:
                                abstract_texts.append(text)
                        return " ".join(abstract_texts).strip()
        return "No abstract available"
    except ET.ParseError as e:
        st.error(f"Error parsing XML for PMID {pmid}: {e}")
        return "Error extracting abstract"


# Function to fetch additional results
async def fetch_additional_results(session: aiohttp.ClientSession, search_query: str, max_results: int, current_count: int) -> List[str]:
    additional_needed = max_results - current_count
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={search_query}&sort=relevance&retmode=json&retmax={additional_needed}&api_key={st.secrets['pubmed_api_key']}"
    
    try:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
            return data['esearchresult'].get('idlist', [])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"Error fetching additional results: {e}")
        return []

# Function to fetch PubMed abstracts
async def pubmed_abstracts(search_terms: str, search_type: str = "all", max_results: int = 5, years_back: int = 3, human_only: bool = False) -> Tuple[List[Dict[str, str]], List[str]]:
    current_year = datetime.now().year
    start_year = current_year - years_back
    human_filter = "+AND+humans[MeSH+Terms]" if human_only else ""
    search_query = f"{search_terms}+AND+{start_year}[PDAT]:{current_year}[PDAT]{human_filter}"
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={search_query}&sort=relevance&retmode=json&retmax={max_results}&api_key={st.secrets['pubmed_api_key']}"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                data = await response.json()
                if 'esearchresult' not in data or 'count' not in data['esearchresult']:
                    st.error("Unexpected response format from PubMed API")
                    return [], []
                if int(data['esearchresult']['count']) == 0:
                    st.write("No PubMed results found within the time period. Expand time range in settings or try a different question.")
                    return [], []

            ids = data['esearchresult'].get('idlist', [])
            if not ids:
                st.write("No results found.")
                return [], []

            articles = []
            unique_urls = set()
            semaphore = asyncio.Semaphore(5)
            tasks = []

            for id in ids:
                details_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={id}&retmode=json&api_key={st.secrets['pubmed_api_key']}"
                abstracts_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={id}&retmode=xml&rettype=abstract&api_key={st.secrets['pubmed_api_key']}"
                tasks.append(fetch_article_details(session, id, details_url, abstracts_url, semaphore))

            results = await asyncio.gather(*tasks)

            for id, details_data, abstracts_data in results:
                if details_data and 'result' in details_data and str(id) in details_data['result']:
                    article = details_data['result'][str(id)]
                    year = article['pubdate'].split(" ")[0]
                    if year.isdigit():
                        abstract = await extract_abstract_from_xml(abstracts_data, id)
                        article_url = f"https://pubmed.ncbi.nlm.nih.gov/{id}"
                        if abstract.strip() and abstract != "No abstract available":
                            articles.append({
                                'title': article['title'],
                                'year': year,
                                'link': article_url,
                                'abstract': abstract.strip()
                            })
                            unique_urls.add(article_url)
                else:
                    st.error(f"Details not available for ID {id}. Details data: {details_data}")

            while len(articles) < max_results:
                additional_ids = await fetch_additional_results(session, search_query, max_results, len(articles))
                if not additional_ids:
                    break

                additional_tasks = []
                for id in additional_ids:
                    details_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={id}&retmode=json&api_key={st.secrets['pubmed_api_key']}"
                    abstracts_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={id}&retmode=xml&rettype=abstract&api_key={st.secrets['pubmed_api_key']}"
                    additional_tasks.append(fetch_article_details(session, id, details_url, abstracts_url, semaphore))

                additional_results = await asyncio.gather(*additional_tasks)

                for id, details_data, abstracts_data in additional_results:
                    if details_data and 'result' in details_data and str(id) in details_data['result']:
                        article = details_data['result'][str(id)]
                        year = article['pubdate'].split(" ")[0]
                        if year.isdigit():
                            abstract = await extract_abstract_from_xml(abstracts_data, id)
                            article_url = f"https://pubmed.ncbi.nlm.nih.gov/{id}"
                            if abstract.strip() and abstract != "No abstract available":
                                articles.append({
                                    'title': article['title'],
                                    'year': year,
                                    'link': article_url,
                                    'abstract': abstract.strip()
                                })
                                unique_urls.add(article_url)
                                if len(articles) >= max_results:
                                    break

        except aiohttp.ClientError as e:
            st.error(f"Error connecting to PubMed API: {e}")
            return [], []
        except Exception as e:
            st.error(f"An unexpected error occurred: {e}")
            return [], []

    return articles[:max_results], list(unique_urls)

# Function to optimize the query using GPT-4o
async def optimize_query(search_terms: str) -> str:
    messages = [
        {"role": "system", "content": main_pubmed_prompt},
        {"role": "user", "content": search_terms}
    ]
    response = create_chat_completion(messages)
    optimized_query = response.choices[0].message.content.strip()
    return optimized_query

def check_password() -> bool:
    """
    Check if the entered password is correct and manage login state.
    Also resets the app when a user successfully logs in.
    """
    # Early return if st.secrets["docker"] == "docker"
    if st.secrets["docker"] == "docker":
        st.session_state.password_correct = True
        return True
    # Initialize session state variables
    if "password" not in st.session_state:
        st.session_state.password = ""
    if "password_correct" not in st.session_state:
        st.session_state.password_correct = False
    if "login_attempts" not in st.session_state:
        st.session_state.login_attempts = 0

    def password_entered() -> None:
        """Callback function when password is entered."""
        if st.session_state["password"] == st.secrets["password"]:
            st.session_state["password_correct"] = True
            st.session_state.login_attempts = 0
            # Reset the app
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False
            st.session_state.login_attempts += 1

    # Check if password is correct
    if not st.session_state["password_correct"]:
        st.text_input("Password", type="password", on_change=password_entered, key='password')
        
        if st.session_state.login_attempts > 0:
            st.error(f"😕 Password incorrect. Attempts: {st.session_state.login_attempts}")
        
        st.write("*Please contact David Liebovitz, MD if you need an updated password for access.*")
        return False

    return True

if "optimized_query" not in st.session_state:
    st.session_state.optimized_query = ""

if "edited_query" not in st.session_state:
    st.session_state.edited_query = ""
    
if "original_query" not in st.session_state:
    st.session_state.original_query = ""
    
if "articles" not in st.session_state:
    st.session_state.articles = []

# Main function to run the Streamlit app
def search_pubmed_page():
    st.title('PubMed Query Formulator')
    
    
    if check_password():
        st.session_state.original_query = st.text_input('Enter your question:')
        scope = st.radio('Select the approach of your search (note - broad may not always returns more total results):', ['narrow; restrict for high relevance', 'broad; expand for related subjects'], index=0, horizontal=True)
        

        submit = st.button('Prepare Your Search')
        if submit and st.session_state.original_query:
            st.session_state.edited_query = ""
            with st.spinner('Optimizing query...'):
                        
                optimized_query = asyncio.run(optimize_query(f'Scope: {scope} Question: {st.session_state.original_query}'))
                st.session_state.optimized_query = optimized_query
        if st.session_state.optimized_query:
            st.write("### PubMed Search Terms:")
            st.write(st.session_state.optimized_query)
            if st.checkbox("Edit search terms"):
                st.session_state.edited_query = st.text_area('Edit the optimized query:', value=st.session_state.optimized_query, height=400)
            else:
                st.session_state.edited_query = st.session_state.optimized_query
                
                # st.markdown(f"**Optimized Query:** {optimized_query}")
        if st.session_state.edited_query:
            pubmed_link = "https://pubmed.ncbi.nlm.nih.gov/?term=" + st.session_state.edited_query.replace(" ", "+")
            st.page_link(pubmed_link, label="**Click here to view in PubMed**", icon="📚")

            st.divider()
            st.write("**Or, perform your search here:**")
            col1, col2 = st.columns([1, 4])
            with col1:
                start_pubmed_search = st.button("Search PubMed Here 😊")
            with col2:
                with st.popover("Options for Searching Here"):
                    search_type = st.selectbox('Search type:', ['all', 'title', 'abstract'], index=0)
                    
                    max_results = st.slider('Maximum number of results:', 1, 20, 5)

                    years_back = st.slider('Years back to search:', 1, 10, 3)

                    human_only = st.checkbox('Limit to Human Studies', value=False)
                
            if start_pubmed_search:
                st.session_state.articles, urls = asyncio.run(pubmed_abstracts(st.session_state.edited_query, search_type, max_results, years_back, human_only))
                if st.session_state.articles:
                    with st.expander("Search used"):
                        st.write(f"**Original Query:** {st.session_state.original_query}")
                        st.write(f"**Edited Query:** {st.session_state.edited_query}")
                        st.write(f"**Search Type:** {search_type}")
                        st.write(f"**Maximum Results:** {max_results}")
                        st.write(f"**Years Back:** {years_back}")
                        st.write(f"**Human Studies Only:** {human_only}")
                articles = st.session_state.articles
                if articles:
                    for article in articles:
                        st.write(f"### [{article['title']}]({article['link']})")
                        st.write(f"**Year:** {article['year']}")
                        st.write(f"**Abstract:** {article['abstract']}")
                else:
                    st.write("No results found.")


search_pubmed_page()
