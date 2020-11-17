import os
import threading
from typing import NoReturn, Optional

import colorcet
import geopandas as gpd
import geoviews as gv
import pandas as pd
import requests
import shapely.geometry as sg
from holoviews.element import Geometry

from layer import Layer


class ResidentialLayer(Layer):
    _census_wards: gpd.GeoDataFrame
    _landuse_polygons: gpd.GeoDataFrame

    def __init__(self):
        super(ResidentialLayer, self).__init__()
        self.key = 'residential'

        self._census_wards_lock = threading.Lock()
        self._census_wards = gpd.GeoDataFrame()
        self._landuse_polygons_lock = threading.Lock()
        self._landuse_polygons = gpd.GeoDataFrame()

    def preload_data(self):
        self.ingest_census_data()

    def generate(self, bounds_polygon: sg.Polygon, from_cache: bool = False) -> Geometry:
        bounds = bounds_polygon.bounds

        if not from_cache:
            self.query_osm_landuse_polygons(bounds_polygon)
        bounded_census_wards = self._census_wards.cx[bounds[1]:bounds[3], bounds[0]:bounds[2]]

        # Find landuse polygons intersecting/within census wards and merge left
        census_df = gpd.overlay(self._landuse_polygons,
                                bounded_census_wards,
                                how='intersection')
        # Estimate the population of landuse polygons from the density of the census ward they are within
        # EPSG:4326 is *not* an equal area projection so would give gibberish areas
        # Project geometries to an equidistant/equal areq projection
        census_df['population'] = census_df['density'] * census_df['geometry'].to_crs('EPSG:4088').area

        # Scale to reduce error for smaller, less dense wards
        # This was found empirically minimising the population error in 10 random villaegs in Hampshire
        def scale_pop(x):
            if 0 < x < 3000:
                return 0.998 * x + 6
            else:
                return x

        # Actually perform the populations scaling
        census_df['population'] = census_df['population'].apply(scale_pop)
        # Construct the GeoViews Polygons
        gv_polys = gv.Polygons(census_df, vdims=['name', 'population']) \
            .opts(color='population',
                  cmap=colorcet.CET_L18,
                  colorbar=True, colorbar_opts={'title': 'Population'}, show_legend=False)

        return gv_polys

    def clear_cache(self):
        self._landuse_polygons = gpd.GeoDataFrame()

    def ingest_census_data(self) -> NoReturn:
        """
        Ingest Census boundaries and density values and overlay/merge
        """
        # Import Census boundaries in Ordnance Survey grid and reproject
        census_wards_df = gpd.read_file(os.sep.join(('static_data', 'england_wa_2011_clipped.shp'))).drop(
            ['altname', 'oldcode'], axis=1).set_crs(
            'EPSG:27700').to_crs('EPSG:4326')
        # Import census ward densities
        density_df = pd.read_csv(os.sep.join(('static_data', 'density.csv')), header=0)
        # Scale from hectares to m^2
        density_df['area'] = density_df['area'] * 10000
        density_df['density'] = density_df['density'] / 10000

        with self._census_wards_lock:
            # These share a common UID, so merge together on it and store
            self._census_wards = census_wards_df.merge(density_df, on='code')

    def query_osm_landuse_polygons(self, bound_poly: sg.Polygon, landuse: Optional[str] = 'residential') -> NoReturn:
        """
        Perform blocking query on OpenStreetMaps Overpass API for objects with the passed landuse.
        Retain only polygons and store in GeoPandas GeoDataFrame
        :param shapely.Polygon bound_poly: bounding box around requested area in EPSG:4326 coordinates
        :param str landuse: OSM landuse key from https://wiki.openstreetmap.org/wiki/Landuse
        """

        bounds = bound_poly.bounds
        overpass_url = "http://overpass-api.de/api/interpreter"
        query = """
               [out:json]
               [timeout:120]
               [bbox:{s_bound}, {w_bound}, {n_bound}, {e_bound}];
               (
                   node[landuse={landuse}];
                   way[landuse={landuse}];
                   rel[landuse={landuse}];
               ); 
               out center body;
               >;
               out center qt;
           """.format(landuse=landuse,
                      s_bound=bounds[0], w_bound=bounds[1], n_bound=bounds[2], e_bound=bounds[3])
        resp = requests.get(overpass_url, params={'data': query})
        data = resp.json()

        ways = [o for o in data['elements'] if o['type'] == 'way']
        nodes = {o['id']: (o['lon'], o['lat']) for o in data['elements'] if o['type'] == 'node'}

        df_list = []
        # Iterate polygons ways
        for element in ways:
            # Find the vertices (AKA nodes) that make up each polygon
            locs = [nodes[id] for id in element['nodes']]
            # Not a polygon if less than 3 vertices, so ignore
            if len(locs) < 3:
                continue
            # Add Shapely polygon to list
            poly = sg.Polygon(locs)
            df_list.append([poly])
        # df_list = [sg.Polygon([nodes[id] for id in element['nodes']]) for element in ways]
        assert len(df_list) > 0
        # OSM uses Web Mercator so set CRS without projecting as CRS is known
        poly_df = gpd.GeoDataFrame(df_list, columns=['geometry']).set_crs('EPSG:4326')

        if landuse not in self._landuse_polygons:
            self._landuse_polygons = poly_df
        else:
            self._landuse_polygons = self._landuse_polygons.append(poly_df)
            self._landuse_polygons.drop_duplicates(subset='geometry', inplace=True, ignore_index=True)