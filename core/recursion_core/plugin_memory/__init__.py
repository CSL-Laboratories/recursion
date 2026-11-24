"""A plugin's saved data, kept by the daemon apart for each plugin and each install.

``words.py`` holds every sentence a person reads about it (one table); ``store.py`` holds the store the
daemon keeps for one plugin and one install identity, reached only through the four ``memory.*`` call
names of the host channel. This package imports nothing at its top so the channel can read the words
table without a cycle.
"""
