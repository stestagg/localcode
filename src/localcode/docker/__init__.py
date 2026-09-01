"""Talking to docker, and the containers localcode spawns.

`client` wraps the docker CLI; `hub` is the long-lived caddy+gitea container a
project runs behind; `agent` is a throwaway container that clones, works and
exits.
"""
