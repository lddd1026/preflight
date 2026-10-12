import litellm

MODEL = "gpt-4o-mini"


def estimate_cost(response):
    return litellm.completion_cost(completion_response=response)
