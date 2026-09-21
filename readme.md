# SiteArmy
Site army is a simple tool that makes it easy to deploy an army of static sites.

This is a random tool I decided to somewhat vibe code in a day to solve my own problems. Spesifically, I do not like maintaining and configuring webservers, and I need to host multiple static sites on a memory constrained vps.

SiteArmy has two features that solve my issues:
- All sites are hosted behind a single caddy server (Not one server per site like some other containers).
- Building the websites, webserver config, and https certs are all handled automatically, just set a url, site type, and a git repo to build from and you are on your way.

SiteArmy supports the following static site generators:
- Raw html/css (not really a site generator but I am counting it).
- Hugo
- Jekyll
- Quartz 5

The app is made in such a way that adding new builders is fairly trivial so I may add support for more static sites.

Tbf, this was done in a couple hours and I don't think many people are going to want it much but I figure it is worth throwing out for those who might have similar needs to me.
