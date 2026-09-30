"""Site-specific public catalog routes. Product parsing remains source scoped."""
from urllib.parse import urlsplit


class CatalogStrategy:
    navigation_first=False
    def seeds(self):return []
    def listing_allowed(self,url):return True
    def sitemap_allowed(self,url):return True


class BeskontaCatalog(CatalogStrategy):
    navigation_first=True
    def seeds(self):return [('sitemap','https://beskonta.ru/sitemap-1.xml'),('listing','https://beskonta.ru/catalog/all/')]
    def listing_allowed(self,url):
        p=urlsplit(url)
        # The published robots policy disallows query pagination. The sitemap
        # supplies individual products, including their embedded executions.
        return not p.query and p.path not in ('/catalog','/catalog/')
    def sitemap_allowed(self,url):return urlsplit(url).path!='/sitemap.xml'


class SensorCatalog(CatalogStrategy):
    navigation_first=True
    def seeds(self):return [('sitemap','https://sensor-com.ru/sitemap/main.xml')]
    def listing_allowed(self,url):
        p=urlsplit(url)
        return not p.query and 'arhiv-produktsii' not in p.path.lower()
    def sitemap_allowed(self,url):
        name=urlsplit(url).path.rsplit('/',1)[-1].lower()
        return name in ('main.xml','category.xml') or name.startswith('goods') and name.endswith('.xml')


class TekoCatalog(CatalogStrategy):
    navigation_first=True
    def seeds(self):return [('sitemap','https://teko-com.ru/sitemap/sitemap.xml'),('listing','https://teko-com.ru/catalog/datchiki/')]
    def listing_allowed(self,url):return '/filter/' not in urlsplit(url).path.lower()
    def sitemap_allowed(self,url):return 'images_sitemap' not in urlsplit(url).path.lower()


STRATEGIES={'beskonta':BeskontaCatalog(),'sensor':SensorCatalog(),'teko':TekoCatalog()}


def strategy_for(source):return STRATEGIES.get(source,CatalogStrategy())
