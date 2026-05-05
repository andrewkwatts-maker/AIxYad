import requests
import json

def check_ollama():
    try:
        response = requests.get("http://localhost:11434/api/tags")
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()
        if data:
            for model in data:
                print(f"Model: {model['name']}, Size: {model['size']}")
        else:
            print("No models found.")
    except requests.exceptions.RequestException as e:
        print(f"Error: Ollama not reachable - {e}")
        return 1
    return 0

if __name__ == "__main__":
    exit_code = check_ollama()
    exit(exit_code)
