#include <stdlib.h>
#include <string.h>

#include "minihttp.h"

static char *dup_range(const char *start, size_t len)
{
  char *out = malloc(len + 1);
  if(!out)
    return NULL;
  memcpy(out, start, len);
  out[len] = '\0';
  return out;
}

/* Split "scheme://host/path" into host and path. Rejects URLs that are
   empty, longer than MH_MAX_URL or missing "://". */
mh_code mh_url_parse(const char *url, char **host, char **path)
{
  const char *sep;
  const char *slash;
  if(!url || !*url || strlen(url) > MH_MAX_URL)
    return MH_URL_MALFORMAT;
  sep = strstr(url, "://");
  if(!sep)
    return MH_URL_MALFORMAT;
  sep += 3;
  slash = strchr(sep, '/');
  *host = dup_range(sep, slash ? (size_t)(slash - sep) : strlen(sep));
  *path = dup_range(slash ? slash : "/", slash ? strlen(slash) : 1);
  if(!*host || !*path)
    return MH_OUT_OF_MEMORY;
  return MH_OK;
}
