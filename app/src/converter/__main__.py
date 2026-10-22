import sys

if len(sys.argv) > 1 and sys.argv[1] == "text-convert":
    from .text_conversion import main

    raise SystemExit(main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "q-source-review":
    from .q_source_review import main

    raise SystemExit(main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "q-to-fx5":
    from .q_to_fx5 import main

    raise SystemExit(main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "map-forward-xy":
    from .map_forward_xy import main

    raise SystemExit(main(sys.argv[2:]))

from .core import main

raise SystemExit(main())
