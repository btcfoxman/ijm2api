"""Search the current Dreamina page's public JS for image ratio enum mappings."""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/app")
from app.browser import CDPBrowser  # noqa: E402


def main(port: int) -> None:
    expression = r'''(async()=>{
      const urls=[...new Set(performance.getEntriesByType('resource').map(x=>x.name)
        .filter(x=>/\.js(?:\?|$)/.test(x)))];
      const found=[];
      for(const url of urls){
        try{
          const source=await (await fetch(url,{credentials:'omit'})).text();
          if(!source.includes('ImageRatioType_219]:{width:')) continue;
          for(const regex of [/ImageRatioType_219\]:\{width:\d+,height:\d+,resolutionType:"[124]k"/g]){
            let match;
            while((match=regex.exec(source))!==null){
              found.push({bundle:new URL(url).pathname.split('/').pop(),
                snippet:source.slice(Math.max(0,match.index-150),Math.min(source.length,match.index+match[0].length+500))});
              if(found.length>=20) return JSON.stringify({script_count:urls.length,found});
            }
          }
        }catch{}
      }
      return JSON.stringify({script_count:urls.length,found});
    })()'''
    result = CDPBrowser(port, timeout=90).evaluate(expression, await_promise=True)
    print(json.dumps(json.loads(result), ensure_ascii=False))


if __name__ == "__main__":
    main(int(sys.argv[1]))
