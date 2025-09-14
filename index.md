---
layout: default
title: Automated News
---

# Automated News

Curated via Inoreader, summarized by an open-source model, and published after manual approval.

<ul>
{% for post in site.posts %}
  <li><a href="{{ post.url | relative_url }}">{{ post.title }}</a> <small>({{ post.date | date: "%b %d, %Y" }})</small></li>
{% endfor %}
</ul>

<p><a href="{{ "/feed.xml" | relative_url }}">RSS feed</a></p>
