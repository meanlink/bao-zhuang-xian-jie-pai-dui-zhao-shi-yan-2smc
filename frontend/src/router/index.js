export function route(){const [page='overview',...segments]=location.hash.replace(/^#\//,'').split('/');return {page,id:segments[0],segments};}
