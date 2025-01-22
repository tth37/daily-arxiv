import os
import datetime
import openai
import arxiv
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
import markdown
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
import requests
from PyPDF2 import PdfReader
import io

load_dotenv()  # Load .env file if you are using one

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")
OPENAI_MINI_MODEL = os.getenv("OPENAI_MINI_MODEL", OPENAI_MODEL)

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

SUBSCRIBERS = os.getenv("SUBSCRIBERS").split(",")  # List of email addresses
TOPICS = os.getenv("TOPICS").split(",")  # List of topics to check

VERBOSE = os.getenv("VERBOSE", "false") == "true"  # Set to true to print debug information
OPENAI_DRYRUN = os.getenv("OPENAI_DRYRUN", "false") == "true"  # Set to true to skip OpenAI API calls
SMTP_DRYRUN = os.getenv("SMTP_DRYRUN", "false") == "true"  # Set to true to skip sending emails

def load_topic(topic):
    """
    Load a Jinja template for the specified topic.
    """
    env = Environment(loader=FileSystemLoader('topics'))
    template = env.get_template(f'{topic}.jinja')
    max_papers = int(template.module.max_papers)
    query = str(template.module.query)
    name = str(template.module.name)
    return template, max_papers, query, name

def fetch_papers(query, max_results=100):
    """
    Fetch newly submitted arXiv papers that match any of the specified keywords,
    restricted to papers submitted 'today'. Returns a list of dictionaries 
    containing paper title, authors, summary (abstract), link, and published date.
    """
    client = arxiv.Client()
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending
    )

    results = []

    for result in client.results(search):
        print(f"\t\tFetching paper: {result.title}")
        paper_pdf_url = result.pdf_url
        reader = PdfReader(io.BytesIO(requests.get(paper_pdf_url).content))
        first_page_text = reader.pages[0].extract_text()
        affiliations = extract_affiliations(first_page_text, result.title)
        paper_info = {
            "title": result.title,
            "authors": [author.name for author in result.authors],
            "affiliations": affiliations,
            "abstract": result.summary.replace("\n", " "),
            "link": result.entry_id,
            "published": str(result.published.date())
        }
        results.append(paper_info)

    return results

def make_completion(prompt, model=OPENAI_MODEL):
    client = openai.Client(
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_BASE_URL
    )
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful research assistant."},
            {"role": "user", "content": prompt}
        ],
    )
    return completion.choices[0].message.content

def generate_report(papers, template):
    """
    Takes a list of paper information dictionaries and uses OpenAI API
    to generate a summary report. Returns the AI-generated text.
    """
    if not papers:
        return "No new papers found today matching your keywords."

    prompt = template.render(papers=papers)
    completion = make_completion(prompt)

    # Extract the AI-generated answer
    report_md = completion.replace("```markdown", "").replace("```md", "").replace("```", "")
    report_html = markdown.markdown(report_md)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        + report_html
        + "</body></html>"
    )

def extract_affiliations(first_page_text, title):
    """
    Extract affiliations from the first page of a paper.
    Using mini model to reduce cost.
    """
    prompt = (
        f"Below is the first page of a scholar paper titled '{title}'."
        " Please extract the affiliations of all authors from the extracted text."
        " Your output should be a list of affiliations in single-line, seperate multiple affiliations with '; '."
        " For repeated affiliations just keep one."
        " If you are unable to extract any affiliations, please write single-line output: None."
        "\n\n"
        f"```\n{first_page_text}\n```"
    )
    completion = make_completion(prompt, model=OPENAI_MINI_MODEL)
    
    # Extract the AI-generated answer
    affiliations = completion.strip().replace("'", "").replace('"', "")
    return affiliations

def send_email(recipients, subject, report):
    """
    Send an email using the specified SMTP server.
    """
    message = MIMEMultipart()
    message["From"] = formataddr(("Daily arXiv Papers", SMTP_USERNAME))
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject

    message.attach(MIMEText(report, "html"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, recipients, message.as_string())

def dump_topic(topic, max_papers, query, name):
    with open(f"logs/{topic}.log", "a", encoding="utf-8") as f:
        f.write(f"Topic name: {name}\n")
        f.write(f"Query: {query}\n")
        f.write(f"Max papers: {max_papers}\n")
        f.write("\n")

def dump_papers(topic, papers, template):
    with open(f"logs/{topic}.log", "a", encoding="utf-8") as f:
        f.write("Papers:\n")
        for paper in papers:
            f.write(f"- {paper['title']} ({paper['published']})\n")
            f.write(f"  Authors: {', '.join(paper['authors'])}\n")
            f.write(f"  Affiliations: {paper['affiliations']}\n")
            f.write(f"  Link: {paper['link']}\n")
            f.write("\n")
        f.write("\n")

def dump_report(topic, report):
    with open(f"logs/{topic}.html", "w", encoding="utf-8") as f:
        f.write(report)

def main():
    if VERBOSE:
        if os.path.exists("logs"):
            for file in os.listdir("logs"):
                os.remove(os.path.join("logs", file))
        if not os.path.exists("logs"):
            os.makedirs("logs")

    today = str(datetime.date.today())

    for topic in TOPICS:
        try:
            print(f"Processing topic: {topic}")
            template, max_papers, query, name = load_topic(topic)
            if VERBOSE:
                dump_topic(topic, max_papers, query, name)
            print(f"\tLoaded topic successfully: {name}")

            papers = fetch_papers(query, max_papers)
            if VERBOSE:
                dump_papers(topic, papers, template)
            print(f"\tFetched {len(papers)} papers from arXiv")

            if OPENAI_DRYRUN:
                print("\tDry run enabled, skipping generation of report")
                continue
            report = generate_report(papers, template)
            if VERBOSE:
                dump_report(topic, report)
            print(f"\tGenerated report for {name} with {OPENAI_MODEL}")

            if SMTP_DRYRUN:
                print("\tDry run enabled, skipping email sending")
                continue
            title = f"📚 Check out daily arXiv papers on {name}"
            send_email(SUBSCRIBERS, title, report)
            print(f"\tEmail sent to {SUBSCRIBERS}")

        except Exception as e:
            print(f"Error processing topic {topic}: {e}")

if __name__ == "__main__":
    main()
