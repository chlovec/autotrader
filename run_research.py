from db.models import ResearchSchedule
from db.session import get_session, init_db
from engine.config import load_config
from engine.research_runner import main

if __name__ == "__main__":
    init_db()
    with get_session() as session:
        selected_count = session.get(ResearchSchedule, 1).selected_count
    main(selected_count=selected_count, config=load_config())
